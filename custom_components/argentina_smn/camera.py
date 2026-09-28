"""Camera platform: precipitation radar (RainViewer) + SMN's own alert zones.

Not sourced from SMN's map servers — see radar.py / const.py for why. The
alert zone polygons drawn on top ARE from SMN though (the same
warning/shortterm data already used by sensor.py), which is what makes the
image useful even when RainViewer's Argentina coverage is thin.
"""
from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, RADAR_UPDATE_INTERVAL, RAINVIEWER_ATTRIBUTION
from .coordinator import ArgentinaSMNDataUpdateCoordinator
from .radar import build_animated_radar_gif

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN radar camera."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    latitude = config_entry.data[CONF_LATITUDE]
    longitude = config_entry.data[CONF_LONGITUDE]
    name = config_entry.data.get(CONF_NAME, "SMN")

    async_add_entities(
        [SMNRadarCamera(coordinator, config_entry, name, latitude, longitude)]
    )


class SMNRadarCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Precipitation radar mosaic with SMN's active alert zones outlined."""

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
        # (image/jpeg), so this has to be set as an instance attribute
        # after calling super().__init__(), not as a class attribute —
        # otherwise it gets overwritten back to jpeg and the frontend
        # fails to render the GIF bytes we actually return.
        self.content_type = "image/gif"
        self._attr_unique_id = f"{config_entry.entry_id}_radar"
        self._attr_name = f"{name} Radar"
        self._latitude = latitude
        self._longitude = longitude
        self._cached_gif: bytes | None = None
        self._cached_at: float = 0.0
        self._config_entry = config_entry

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the cached radar+alerts GIF, rebuilding it if stale."""
        now = time.monotonic()
        if self._cached_gif and (now - self._cached_at) < RADAR_UPDATE_INTERVAL:
            return self._cached_gif

        session = async_get_clientsession(self.hass)
        alerts = self.coordinator.data.shortterm_alerts if self.coordinator.data else []
        try:
            gif = await build_animated_radar_gif(
                session, self._latitude, self._longitude, alerts=alerts
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error building radar GIF: %s", err, exc_info=True)
            gif = None

        if gif:
            self._cached_gif = gif
            self._cached_at = now
            return gif

        if not self._cached_gif:
            _LOGGER.warning(
                "No radar image available yet for %s,%s (RainViewer fetch failed "
                "and no cached frame exists)",
                self._latitude,
                self._longitude,
            )
        # Fall back to the last good frame rather than a broken image.
        return self._cached_gif
