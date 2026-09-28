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
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import RADAR_UPDATE_INTERVAL, RAINVIEWER_ATTRIBUTION
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
        self._attr_unique_id = f"{config_entry.entry_id}_radar"
        self._attr_name = f"{name} Radar"
        self._latitude = latitude
        self._longitude = longitude
        self._cached_gif: bytes | None = None
        self._cached_at: float = 0.0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the cached animated GIF, rebuilding it if stale."""
        now = time.monotonic()
        if self._cached_gif and (now - self._cached_at) < RADAR_UPDATE_INTERVAL:
            return self._cached_gif

        session = async_get_clientsession(self.hass)
        gif = await build_animated_radar_gif(session, self._latitude, self._longitude)

        if gif:
            self._cached_gif = gif
            self._cached_at = now
            return gif

        # Fall back to the last good frame rather than a broken image.
        return self._cached_gif
