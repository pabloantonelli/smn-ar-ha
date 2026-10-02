"""Camera platform: precipitation radar (RainViewer) + SMN's own alert zones,
plus NASA GIBS satellite imagery (local/country/province, GeoColor/infrared,
static/animated).

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

from .boundaries import (
    get_all_province_rings,
    get_country_rings,
    get_department_rings,
    get_province_rings,
)
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

    entities: list[Camera] = [
        SMNRadarCamera(coordinator, config_entry, name, latitude, longitude),
    ]
    for force_infrared in (False, True):
        entities.append(
            SMNSatelliteCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteAnimationCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteCountryCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteCountryAnimationCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteProvinceCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteProvinceAnimationCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
    async_add_entities(entities)


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


class _SMNSatelliteCameraBase(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Shared plumbing for every GIBS-backed camera below.

    Handles the parts identical across all of them: device_info, the
    background refresh loop/interval, and the "build once, serve from
    cache" pattern already used by SMNRadarCamera (see its docstring for
    why). Subclasses only implement `_build_image()`.
    """

    _attr_has_entity_name = True
    _attr_attribution = GIBS_ATTRIBUTION
    content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        unique_id_suffix: str,
        name_suffix: str,
        update_interval: int,
        translation_key: str,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = self.__class__.content_type
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{config_entry.entry_id}{unique_id_suffix}"
        self._attr_name = name_suffix
        self._config_entry = config_entry
        self._update_interval = update_interval
        self._cached_image: bytes | None = None
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
                timedelta(seconds=self._update_interval),
            )
        )

    async def _async_scheduled_refresh(self, _now=None) -> None:
        await self._async_refresh()

    async def _build_image(self) -> bytes | None:
        raise NotImplementedError

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            image = await self._build_image()
            if image:
                self._cached_image = image
            elif not self._cached_image:
                _LOGGER.warning(
                    "No image available yet for %s (GIBS fetch failed and no "
                    "cached frame exists)",
                    self.entity_id or self._attr_unique_id,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building image for %s: %s", self._attr_unique_id, err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._cached_image is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_image


class SMNSatelliteCamera(_SMNSatelliteCameraBase):
    """Latest NASA GIBS GOES-East satellite frame, with an estimated cloud-drift arrow.

    Static JPEG for the same attachment-compatibility reason as the radar
    camera — see SMNRadarCamera's docstring. GeoColor by day, clean
    infrared by night (see satellite.py's _layer_for) — unless
    `force_infrared` is set, which pins it to infrared always (used for
    the separate "Satélite Infrarrojo" entity, so IR is available as its
    own view rather than only as the automatic night fallback).
    """

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = "_satellite_infrared" if force_infrared else "_satellite"
        label = "Satélite Infrarrojo" if force_infrared else "Satélite"
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_UPDATE_INTERVAL,
            "satellite_infrared" if force_infrared else "satellite",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        session = async_get_clientsession(self.hass)
        return await build_satellite_snapshot_jpeg(
            session,
            self._latitude,
            self._longitude,
            is_up(self.hass),
            force_infrared=self._force_infrared,
            current_weather=current_weather,
        )


class SMNSatelliteAnimationCamera(_SMNSatelliteCameraBase):
    """Animated GIF of the last hour of NASA GIBS satellite frames.

    Deliberately a GIF, unlike the static cameras — not meant to be
    attached via a notification integration's "camera snapshot" feature
    (same JPEG-only limitation noted on SMNRadarCamera), but shared as its
    entity_picture URL (HA signs it automatically), which any chat client
    fetches and renders as an actual animation.

    Refreshed less often than the still cameras: each build fetches a full
    tile grid per frame (SATELLITE_ANIMATION_FRAMES times the work of the
    still satellite camera), so it runs on a longer interval.
    """

    content_type = "image/gif"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = "_satellite_infrared_animation" if force_infrared else "_satellite_animation"
        label = "Satélite Infrarrojo (animado)" if force_infrared else "Satélite (animado)"
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_ANIMATION_UPDATE_INTERVAL,
            "satellite_infrared_animation" if force_infrared else "satellite_animation",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        session = async_get_clientsession(self.hass)
        return await build_satellite_animation_gif(
            session,
            self._latitude,
            self._longitude,
            is_up(self.hass),
            force_infrared=self._force_infrared,
            current_weather=current_weather,
        )


class SMNSatelliteCountryCamera(_SMNSatelliteCameraBase):
    """Whole-Argentina satellite view, zoomed to fit the country's own outline.

    Unlike SMNSatelliteCamera (fixed zoom around one lat/lon), the zoom
    here is computed from the country outline's bounding box — see
    satellite.py's build_region_snapshot_jpeg — so all of Argentina is
    actually visible, not just the ~700km around the configured location.
    The configured location is still marked on it (a small pin, see
    satellite.py's _draw_location_pin), since it's usually a single point
    somewhere inside that whole-country frame.
    """

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = "_satellite_country_infrared" if force_infrared else "_satellite_country"
        label = "Satélite Argentina Infrarrojo" if force_infrared else "Satélite Argentina"
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_REGION_UPDATE_INTERVAL,
            "satellite_country_infrared" if force_infrared else "satellite_country",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        session = async_get_clientsession(self.hass)
        return await build_region_snapshot_jpeg(
            session,
            get_country_rings(),
            is_up(self.hass),
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=current_weather,
            subdivisions=get_all_province_rings(),
        )


class SMNSatelliteCountryAnimationCamera(_SMNSatelliteCameraBase):
    """Animated GIF (last hour) of the whole-Argentina satellite view.

    Same idea as SMNSatelliteAnimationCamera but zoomed to the whole
    country instead of the fixed local zoom — see SMNSatelliteCountryCamera.
    """

    content_type = "image/gif"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = (
            "_satellite_country_infrared_animation"
            if force_infrared
            else "_satellite_country_animation"
        )
        label = (
            "Satélite Argentina Infrarrojo (animado)"
            if force_infrared
            else "Satélite Argentina (animado)"
        )
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_REGION_ANIMATION_UPDATE_INTERVAL,
            "satellite_country_infrared_animation"
            if force_infrared
            else "satellite_country_animation",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        session = async_get_clientsession(self.hass)
        return await build_region_animation_gif(
            session,
            get_country_rings(),
            is_up(self.hass),
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=current_weather,
            subdivisions=get_all_province_rings(),
        )


class SMNSatelliteProvinceCamera(_SMNSatelliteCameraBase):
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

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = "_satellite_province_infrared" if force_infrared else "_satellite_province"
        label = "Satélite provincia Infrarrojo" if force_infrared else "Satélite provincia"
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_REGION_UPDATE_INTERVAL,
            "satellite_province_infrared" if force_infrared else "satellite_province",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        province = (current_weather or {}).get("province")
        rings = get_province_rings(province) if province else None
        if not rings:
            _LOGGER.debug(
                "No matching province outline yet for %r, skipping refresh", province
            )
            return None

        session = async_get_clientsession(self.hass)
        return await build_region_snapshot_jpeg(
            session,
            rings,
            is_up(self.hass),
            fill_factor=SATELLITE_PROVINCE_FILL_FACTOR,
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=current_weather,
            subdivisions=get_department_rings(province),
        )


class SMNSatelliteProvinceAnimationCamera(_SMNSatelliteCameraBase):
    """Animated GIF (last hour) zoomed to fit the configured location's province.

    Same idea as SMNSatelliteAnimationCamera (shared as a URL, not meant
    for attachment — see its docstring) but at the province's own
    zoom-to-fit level instead of the fixed local zoom, and with the extra
    SATELLITE_PROVINCE_FILL_FACTOR zoom bump the static province camera
    also uses.
    """

    content_type = "image/gif"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        suffix = (
            "_satellite_province_infrared_animation"
            if force_infrared
            else "_satellite_province_animation"
        )
        label = (
            "Satélite provincia Infrarrojo (animado)"
            if force_infrared
            else "Satélite provincia (animado)"
        )
        super().__init__(
            coordinator,
            config_entry,
            suffix,
            f"{name} {label}",
            SATELLITE_REGION_ANIMATION_UPDATE_INTERVAL,
            "satellite_province_infrared_animation"
            if force_infrared
            else "satellite_province_animation",
        )
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared

    async def _build_image(self) -> bytes | None:
        from homeassistant.helpers.sun import is_up

        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        province = (current_weather or {}).get("province")
        rings = get_province_rings(province) if province else None
        if not rings:
            _LOGGER.debug(
                "No matching province outline yet for %r, skipping refresh", province
            )
            return None

        session = async_get_clientsession(self.hass)
        return await build_region_animation_gif(
            session,
            rings,
            is_up(self.hass),
            fill_factor=SATELLITE_PROVINCE_FILL_FACTOR,
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=current_weather,
            subdivisions=get_department_rings(province),
        )
