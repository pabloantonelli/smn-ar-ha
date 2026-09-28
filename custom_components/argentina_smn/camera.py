"""Camera platform exposing an animated precipitation radar (RainViewer).

Not sourced from SMN — see radar.py / const.py for why.
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

from .const import DOMAIN, RADAR_UPDATE_INTERVAL, RAINVIEWER_ATTRIBUTION
from .radar import build_animated_radar_gif

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN radar camera."""
    latitude = config_entry.data[CONF_LATITUDE]
    longitude = config_entry.data[CONF_LONGITUDE]
    name = config_entry.data.get(CONF_NAME, "SMN")

    async_add_entities([SMNRadarCamera(config_entry, name, latitude, longitude)])


class SMNRadarCamera(Camera):
    """Animated precipitation radar mosaic around the configured location."""

    _attr_has_entity_name = True
    _attr_translation_key = "radar"
    _attr_attribution = RAINVIEWER_ATTRIBUTION

    def __init__(
        self,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
    ) -> None:
        super().__init__()
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
        """Return the cached animated GIF, rebuilding it if stale."""
        now = time.monotonic()
        if self._cached_gif and (now - self._cached_at) < RADAR_UPDATE_INTERVAL:
            return self._cached_gif

        session = async_get_clientsession(self.hass)
        try:
            gif = await build_animated_radar_gif(session, self._latitude, self._longitude)
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
