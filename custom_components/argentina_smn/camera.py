"""Camera platform: precipitation radar (RainViewer) + SMN's own alert zones.

Not sourced from SMN's map servers — see radar.py / const.py for why. The
alert zone polygons drawn on top ARE from SMN though (the same
warning/shortterm data already used by sensor.py), which is what makes the
image useful even when RainViewer's Argentina coverage is thin.
"""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .boundaries import get_country_rings, get_province_rings
from .const import (
    DOMAIN,
    GIBS_ATTRIBUTION,
    RADAR_UPDATE_INTERVAL,
    RAINVIEWER_ATTRIBUTION,
    SATELLITE_ANIMATION_UPDATE_INTERVAL,
    SATELLITE_PROVINCE_FILL_FACTOR,
    SATELLITE_REGION_ANIMATION_UPDATE_INTERVAL,
    SATELLITE_REGION_UPDATE_INTERVAL,
    SATELLITE_UPDATE_INTERVAL,
)
from .coordinator import ArgentinaSMNDataUpdateCoordinator
from .radar import build_radar_snapshot_jpeg
from .satellite import (
    build_region_animation_gif,
    build_region_snapshot_jpeg,
    build_satellite_animation_gif,
    build_satellite_snapshot_jpeg,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN radar and satellite cameras."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    latitude = config_entry.data[CONF_LATITUDE]
    longitude = config_entry.data[CONF_LONGITUDE]
    name = config_entry.data.get(CONF_NAME, "SMN")

    async_add_entities(
        [
            SMNRadarCamera(coordinator, config_entry, name, latitude, longitude),
            SMNSatelliteCamera(coordinator, config_entry, name, latitude, longitude),
            SMNSatelliteCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared=True
            ),
            SMNSatelliteAnimationCamera(
                coordinator, config_entry, name, latitude, longitude
            ),
            SMNSatelliteCountryCamera(coordinator, config_entry, name),
            SMNSatelliteProvinceCamera(coordinator, config_entry, name),
            SMNSatelliteProvinceAnimationCamera(coordinator, config_entry, name),
        ]
    )


class SMNRadarCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Precipitation radar mosaic with SMN's active alert zones outlined.

    A static JPEG (not an animated GIF): most notification integrations
    that let you attach a "camera" entity (Telegram, WhatsApp-via-Baileys,
    etc.) assume a plain photo, the same way HA's own default camera
    content type is JPEG. An animated GIF gets silently rejected or
    mis-typed by several of those. See radar.py's docstring for the full
    reasoning.

    The JPEG is rebuilt proactively in the background every
    RADAR_UPDATE_INTERVAL, not lazily on first request — building it
    (roughly two dozen tile fetches, even fully parallelized) can take a
    few seconds, which is enough to blow past the short timeout some
    notification services use when attaching a camera snapshot. With a
    background refresh, async_camera_image almost always just returns an
    already-built image instantly.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "radar"
    _attr_attribution = RAINVIEWER_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        # Camera.__init__ sets self.content_type = DEFAULT_CONTENT_TYPE
        # already (image/jpeg), so this is redundant, but kept explicit
        # since past bugs here came from setting it as a class attribute
        # instead of an instance one after super().__init__().
        self.content_type = "image/jpeg"
        self._attr_unique_id = f"{config_entry.entry_id}_radar"
        self._attr_name = f"{name} Radar"
        self._latitude = latitude
        self._longitude = longitude
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Start the background refresh loop once the entity is registered."""
        await super().async_added_to_hass()

        # Build the first frame right away instead of waiting a full
        # RADAR_UPDATE_INTERVAL for the periodic refresh below.
        self.hass.async_create_task(self._async_refresh())

        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=RADAR_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        """Rebuild the cached JPEG in the background."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            session = async_get_clientsession(self.hass)
            data = self.coordinator.data
            # Nationwide list, not the per-location one: see
            # build_radar_snapshot_jpeg's docstring for why (SMN's own
            # per-location filter is stricter than what's actually visible
            # on the map).
            alerts = data.nationwide_shortterm_alerts if data else []
            current_weather = data.current_weather_data if data else None
            hourly_forecast = data.hourly_forecast if data else None
            image = await build_radar_snapshot_jpeg(
                session,
                self._latitude,
                self._longitude,
                alerts=alerts,
                current_weather=current_weather,
                hourly_forecast=hourly_forecast,
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No radar image available yet for %s,%s (RainViewer "
                    "fetch failed and no cached frame exists)",
                    self._latitude,
                    self._longitude,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error building radar image: %s", err, exc_info=True)
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the cached radar+alerts JPEG.

        Normally this is already warm from the background refresh loop. If
        it's genuinely the very first call and that hasn't completed yet,
        build it synchronously this once rather than return nothing.
        """
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image


class SMNSatelliteCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Latest NASA GIBS GOES-East satellite frame, with an estimated cloud-drift arrow.

    Static JPEG for the same attachment-compatibility reason as the radar
    camera — see SMNRadarCamera's docstring. GeoColor by day, clean
    infrared by night (see satellite.py's _layer_for) — unless
    `force_infrared` is set, which pins it to infrared always (used for
    the separate "Satélite Infrarrojo" entity, so IR is available as its
    own view rather than only as the automatic night fallback).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "satellite"
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/jpeg"
        self._force_infrared = force_infrared
        suffix = "_satellite_infrared" if force_infrared else "_satellite"
        label = "Satélite Infrarrojo" if force_infrared else "Satélite"
        if force_infrared:
            self._attr_translation_key = "satellite_infrared"
        self._attr_unique_id = f"{config_entry.entry_id}{suffix}"
        self._attr_name = f"{name} {label}"
        self._latitude = latitude
        self._longitude = longitude
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=SATELLITE_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            from homeassistant.helpers.sun import is_up

            session = async_get_clientsession(self.hass)
            image = await build_satellite_snapshot_jpeg(
                session,
                self._latitude,
                self._longitude,
                is_up(self.hass),
                force_infrared=self._force_infrared,
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No satellite image available yet for %s,%s (GIBS fetch "
                    "failed and no cached frame exists)",
                    self._latitude,
                    self._longitude,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error building satellite image: %s", err, exc_info=True)
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image


class SMNSatelliteAnimationCamera(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera
):
    """Animated GIF of the last hour of NASA GIBS satellite frames.

    Deliberately a GIF, unlike the other two cameras here — this one isn't
    meant to be attached via a notification integration's "camera snapshot"
    feature (same JPEG-only limitation noted on SMNRadarCamera), but shared
    as its entity_picture URL (HA signs it automatically), which any chat
    client fetches and renders as an actual animation.

    Refreshed less often than the still cameras: each build fetches a full
    tile grid per frame (SATELLITE_ANIMATION_FRAMES times the work of the
    still satellite camera), so it runs on a longer interval.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "satellite_animation"
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/gif"
        self._attr_unique_id = f"{config_entry.entry_id}_satellite_animation"
        self._attr_name = f"{name} Satélite (animado)"
        self._latitude = latitude
        self._longitude = longitude
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=SATELLITE_ANIMATION_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            from homeassistant.helpers.sun import is_up

            session = async_get_clientsession(self.hass)
            image = await build_satellite_animation_gif(
                session, self._latitude, self._longitude, is_up(self.hass)
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No satellite animation available yet for %s,%s (GIBS "
                    "fetch failed and no cached frames exist)",
                    self._latitude,
                    self._longitude,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building satellite animation: %s", err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image



class SMNSatelliteCountryCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Whole-Argentina satellite view, zoomed to fit the country's own outline.

    Unlike SMNSatelliteCamera (fixed zoom around one lat/lon), the zoom
    here is computed from the country outline's bounding box — see
    satellite.py's build_region_snapshot_jpeg — so all of Argentina is
    actually visible, not just the ~700km around the configured location.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "satellite_country"
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/jpeg"
        self._attr_unique_id = f"{config_entry.entry_id}_satellite_country"
        self._attr_name = f"{name} Satélite Argentina"
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=SATELLITE_REGION_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            from homeassistant.helpers.sun import is_up

            session = async_get_clientsession(self.hass)
            image = await build_region_snapshot_jpeg(
                session, get_country_rings(), is_up(self.hass)
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No country satellite image available yet (GIBS fetch "
                    "failed and no cached frame exists)"
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building country satellite image: %s", err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image


class SMNSatelliteProvinceCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Whole-province satellite view, zoomed to fit the configured location's province.

    The province is whatever SMN resolves for the configured location
    (coordinator.data.current_weather_data["province"], the same field the
    other cameras' fallback logic checks) — not user-configurable here, so
    this camera just tracks wherever the integration itself is set up for.
    Until that province is known and matches the bundled outline dataset
    (see boundaries.py), there's nothing to build; the entity simply has
    no image yet rather than falling back to the country view, which would
    be confusing under a "province" name.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "satellite_province"
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/jpeg"
        self._attr_unique_id = f"{config_entry.entry_id}_satellite_province"
        self._attr_name = f"{name} Satélite provincia"
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=SATELLITE_REGION_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            from homeassistant.helpers.sun import is_up

            data = self.coordinator.data
            current_weather = data.current_weather_data if data else None
            province = (current_weather or {}).get("province")
            rings = get_province_rings(province) if province else None
            if not rings:
                _LOGGER.debug(
                    "No matching province outline yet for %r, skipping refresh", province
                )
                return

            session = async_get_clientsession(self.hass)
            image = await build_region_snapshot_jpeg(
                session, rings, is_up(self.hass), fill_factor=SATELLITE_PROVINCE_FILL_FACTOR
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No province satellite image available yet for %s (GIBS "
                    "fetch failed and no cached frame exists)",
                    province,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building province satellite image: %s", err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image



class SMNSatelliteProvinceAnimationCamera(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera
):
    """Animated GIF (last hour) zoomed to fit the configured location's province.

    Same idea as SMNSatelliteAnimationCamera (shared as a URL, not meant
    for attachment — see its docstring) but at the province's own
    zoom-to-fit level instead of the fixed local zoom, and with the extra
    SATELLITE_PROVINCE_FILL_FACTOR zoom bump the static province camera
    also uses.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "satellite_province_animation"
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/gif"
        self._attr_unique_id = f"{config_entry.entry_id}_satellite_province_animation"
        self._attr_name = f"{name} Satélite provincia (animado)"
        self._cached_image: bytes | None = None
        self._config_entry = config_entry
        self._refreshing = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_refresh())
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=SATELLITE_REGION_ANIMATION_UPDATE_INTERVAL),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            from homeassistant.helpers.sun import is_up

            data = self.coordinator.data
            current_weather = data.current_weather_data if data else None
            province = (current_weather or {}).get("province")
            rings = get_province_rings(province) if province else None
            if not rings:
                _LOGGER.debug(
                    "No matching province outline yet for %r, skipping refresh", province
                )
                return

            session = async_get_clientsession(self.hass)
            image = await build_region_animation_gif(
                session, rings, is_up(self.hass), fill_factor=SATELLITE_PROVINCE_FILL_FACTOR
            )
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No province satellite animation available yet for %s "
                    "(GIBS fetch failed and no cached frames exist)",
                    province,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building province satellite animation: %s", err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image
