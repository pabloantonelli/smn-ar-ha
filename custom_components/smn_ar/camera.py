"""Camera platform: precipitation radar (RainViewer) + SMN's alert zones,
and animated NASA GIBS satellite views of the country and of the configured
location's province (color and infrared).

Not sourced from SMN's map servers — see radar.py / const.py for why. The
alert zone polygons drawn on the radar ARE from SMN though (the same
warning/shortterm data used by sensor.py).
"""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
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
)
from .coordinator import ArgentinaSMNDataUpdateCoordinator
from .radar import build_radar_snapshot_jpeg
from .satellite import build_region_animation_frames, encode_mp4

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the radar camera and the four animated satellite cameras."""
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
            SMNSatelliteCountryCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
        entities.append(
            SMNSatelliteProvinceCamera(
                coordinator, config_entry, name, latitude, longitude, force_infrared
            )
        )
    _remove_stale_cameras(hass, config_entry, {entity.unique_id for entity in entities})
    async_add_entities(entities)


def _remove_stale_cameras(
    hass: HomeAssistant, config_entry: ConfigEntry, current_unique_ids: set[str | None]
) -> None:
    """Drop camera entities earlier versions created (static, local and video variants)."""
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain == "camera" and entry.unique_id not in current_unique_ids:
            registry.async_remove(entry.entity_id)


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




class _SMNSatelliteAnimationCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Animated satellite view, served as a short looping MP4.

    Built in the background and served from cache, so a request never waits
    on GIBS (the radar camera does the same — see its docstring).
    """

    _attr_has_entity_name = True
    _attr_attribution = GIBS_ATTRIBUTION

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        kind: str,
        label: str,
        latitude: float,
        longitude: float,
        force_infrared: bool,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "video/mp4"
        self._attr_translation_key = kind
        self._attr_unique_id = f"{config_entry.entry_id}_{kind}"
        self._attr_name = f"{name} {label}"
        self._config_entry = config_entry
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared
        self._video: bytes | None = None
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

    async def _build_frames(self) -> list[Any] | None:
        raise NotImplementedError

    async def _async_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            frames = await self._build_frames()
            if frames:
                video = await self.hass.async_add_executor_job(encode_mp4, frames)
                if video:
                    self._video = video
            elif not self._video:
                _LOGGER.warning(
                    "No satellite animation available yet for %s (GIBS fetch failed)",
                    self.entity_id or self._attr_unique_id,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error building animation for %s: %s", self._attr_unique_id, err, exc_info=True
            )
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        if self._video is None and not self._refreshing:
            await self._async_refresh()
        return self._video


class SMNSatelliteCountryCamera(_SMNSatelliteAnimationCamera):
    """Animation of all of Argentina, with province borders and the location pin."""

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
        force_infrared: bool = False,
    ) -> None:
        super().__init__(
            coordinator,
            config_entry,
            name,
            "satellite_country_infrared" if force_infrared else "satellite_country",
            "Satélite Argentina Infrarrojo" if force_infrared else "Satélite Argentina",
            latitude,
            longitude,
            force_infrared,
        )

    async def _build_frames(self) -> list[Any] | None:
        data = self.coordinator.data
        return await build_region_animation_frames(
            async_get_clientsession(self.hass),
            get_country_rings(),
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=data.current_weather_data if data else None,
            subdivisions=get_all_province_rings(),
        )


class SMNSatelliteProvinceCamera(_SMNSatelliteAnimationCamera):
    """Animation of the configured location's province, with its departments.

    The province is whatever SMN resolves for the configured location
    (current_weather_data["province"]); until it's known and matches the
    bundled outlines (boundaries.py), there's no image rather than a
    fallback to the country view, which would be misleading under this name.
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
        super().__init__(
            coordinator,
            config_entry,
            name,
            "satellite_province_infrared" if force_infrared else "satellite_province",
            "Satélite provincia Infrarrojo" if force_infrared else "Satélite provincia",
            latitude,
            longitude,
            force_infrared,
        )

    async def _build_frames(self) -> list[Any] | None:
        data = self.coordinator.data
        current_weather = data.current_weather_data if data else None
        province = (current_weather or {}).get("province")
        rings = get_province_rings(province) if province else None
        if not rings:
            _LOGGER.debug("No matching province outline yet for %r, skipping refresh", province)
            return None
        return await build_region_animation_frames(
            async_get_clientsession(self.hass),
            rings,
            force_infrared=self._force_infrared,
            pin=(self._latitude, self._longitude),
            current_weather=current_weather,
            subdivisions=get_department_rings(province),
        )

