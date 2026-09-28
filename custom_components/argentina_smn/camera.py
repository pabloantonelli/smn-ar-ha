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
    """Precipitation radar mosaic with SMN's active alert zones outlined.

    The GIF is rebuilt proactively in the background every
    RADAR_UPDATE_INTERVAL, not lazily on first request — building it
    (roughly two dozen tile fetches, even fully parallelized) can take a
    few seconds, which is enough to blow past the short timeout some
    notification services use when attaching a camera snapshot. With a
    background refresh, async_camera_image almost always just returns an
    already-built GIF instantly.
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
        """Rebuild the cached GIF in the background."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            session = async_get_clientsession(self.hass)
            # Nationwide list, not the per-location one: see
            # build_animated_radar_gif's docstring for why (SMN's own
            # per-location filter is stricter than what's actually visible
            # on the map).
            alerts = (
                self.coordinator.data.nationwide_shortterm_alerts
                if self.coordinator.data
                else []
            )
            gif = await build_animated_radar_gif(
                session, self._latitude, self._longitude, alerts=alerts
            )
            if gif:
                self._cached_gif = gif
            elif not self._cached_gif:
                _LOGGER.warning(
                    "No radar image available yet for %s,%s (RainViewer "
                    "fetch failed and no cached frame exists)",
                    self._latitude,
                    self._longitude,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error building radar GIF: %s", err, exc_info=True)
        finally:
            self._refreshing = False

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the cached radar+alerts GIF.

        Normally this is already warm from the background refresh loop. If
        it's genuinely the very first call and that hasn't completed yet,
        build it synchronously this once rather than return nothing.
        """
        if self._cached_gif is None and not self._refreshing:
            await self._async_refresh()
        return self._cached_gif
