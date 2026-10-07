"""Camera platform: animated precipitation radar (RainViewer) + SMN's alert
zones, a map of the country's avisos, and animated NASA GIBS satellite views
of the country and of the configured location's province (color and infrared).

Not sourced from SMN's map servers — see radar.py / const.py for why. The
alert zone polygons drawn on the radar ARE from SMN though (the same
warning/shortterm data used by sensor.py).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from pathlib import Path
import time
from typing import Any

from aiohttp import web

from homeassistant.components.camera import Camera
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import (
    KEY_AUTHENTICATED,
    HomeAssistantView,
    StaticPathConfig,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .boundaries import (
    get_all_province_rings,
    get_country_rings,
    get_department_rings,
    get_province_rings,
    preload_boundaries,
)
from .const import (
    DOMAIN,
    GIBS_ATTRIBUTION,
    RADAR_UPDATE_INTERVAL,
    RAINVIEWER_ATTRIBUTION,
    SATELLITE_ANIMATION_UPDATE_INTERVAL,
)
from .coordinator import ArgentinaSMNData, ArgentinaSMNDataUpdateCoordinator
from .alerts_map import build_country_alerts_jpeg
from .radar import RadarLayers, fetch_radar_layers, render_radar_frames
from .satellite import build_region_animation_frames, encode_gif, encode_mp4

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.http import KEY_HASS
except ImportError:  # Home Assistant < 2024.2
    KEY_HASS = "hass"

_VIDEOS_KEY = f"{DOMAIN}_videos"
_FRONTEND_KEY = f"{DOMAIN}_frontend"
_CARD_URL = f"/{DOMAIN}/smn-ar-video-card.js"


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the MP4 endpoint and the video card's JS, once per HA run."""
    if hass.data.get(_FRONTEND_KEY):
        return
    hass.data[_FRONTEND_KEY] = True
    hass.http.register_view(SMNSatelliteVideoView())
    card_path = Path(__file__).parent / "frontend" / "smn-ar-video-card.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARD_URL, str(card_path), False)]
    )
    version = (await hass.async_add_executor_job(card_path.stat)).st_mtime_ns
    add_extra_js_url(hass, f"{_CARD_URL}?v={version}")


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
    await hass.async_add_executor_job(preload_boundaries)
    await _async_register_frontend(hass)

    entities: list[Camera] = [
        SMNRadarCamera(coordinator, config_entry, name, latitude, longitude),
        SMNCountryAlertsCamera(coordinator, config_entry, name, latitude, longitude),
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


# How long a request for the image waits for a rebuild that includes
# just-changed avisos before settling for the previous frame. Home
# Assistant's own snapshot timeout is 10s.
_FRESH_IMAGE_WAIT = 8


def _alerts_key(alerts: list[dict[str, Any]]) -> frozenset[tuple[Any, ...]]:
    """Which avisos an image was drawn with, to tell when it's out of date."""
    return frozenset(ArgentinaSMNData._shortterm_alert_signature(alert) for alert in alerts)


class _SMNVideoMixin:
    """An animated camera's MP4, served by SMNSatelliteVideoView.

    Dashboards render the camera image in an <img>, which only Safari can
    play an MP4 in, so the camera image is a GIF and the smoother, lighter
    looping MP4 is what `entity_picture` points to. Notifiers that send
    whatever `entity_picture` serves get the video, and the bundled
    smn-ar-video-card (frontend/) plays it in a loop on the dashboard.
    Goes first among a camera's bases, so `super()` here reaches Camera.
    """

    video: bytes | None = None
    _video_built_at: datetime | None = None

    @property
    def entity_picture(self) -> str:
        """The MP4 once built, else the default still."""
        if not self.video:
            return super().entity_picture
        return f"/api/{DOMAIN}/video/{self.entity_id}?token={self.access_tokens[-1]}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """When the animation was last rebuilt (entity_picture also changes as its token rotates)."""
        if not self._video_built_at:
            return {}
        return {"animation_updated": self._video_built_at.isoformat()}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(_VIDEOS_KEY, {})[self.entity_id] = self
        self.async_on_remove(lambda: self.hass.data[_VIDEOS_KEY].pop(self.entity_id, None))

    async def _async_encode_video(self, frames: list[Any]) -> None:
        video = await self.hass.async_add_executor_job(encode_mp4, frames)
        if video:
            self.video = video
            self._video_built_at = dt_util.utcnow()
            self.async_write_ha_state()


class _SMNAlertsImageCamera(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera):
    """Image with SMN's aviso polygons, rebuilt in the background.

    Rebuilt every RADAR_UPDATE_INTERVAL, and also right away when a
    coordinator update brings a different set of avisos. Without the
    latter, an automation that fires when an alert turns on and attaches
    this camera's image (WhatsApp, Telegram, etc.) could get a frame drawn
    before the aviso existed, with no polygon. A request that arrives while
    that rebuild runs waits for it (up to _FRESH_IMAGE_WAIT).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        # Camera.__init__ sets self.content_type = DEFAULT_CONTENT_TYPE
        # already (image/jpeg), so this is redundant, but kept explicit
        # since past bugs here came from setting it as a class attribute
        # instead of an instance one after super().__init__().
        self.content_type = "image/jpeg"
        self._config_entry = config_entry
        self._image: bytes | None = None
        self._image_alerts_key: frozenset[tuple[Any, ...]] | None = None
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _current_alerts(self) -> list[dict[str, Any]]:
        # Nationwide list, not the per-location one: see
        # build_radar_snapshot_jpeg's docstring for why (SMN's own
        # per-location filter is stricter than what's actually visible
        # on the map).
        data = self.coordinator.data
        return data.nationwide_shortterm_alerts if data else []

    async def _async_build_image(self, alerts: list[dict[str, Any]]) -> bytes | None:
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        """Build the first frame right away, then keep it fresh in the background."""
        await super().async_added_to_hass()
        self._async_start_refresh()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_scheduled_refresh,
                timedelta(seconds=RADAR_UPDATE_INTERVAL),
            )
        )

    @callback
    def _async_scheduled_refresh(self, _now: datetime | None = None) -> None:
        self._async_start_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        if _alerts_key(self._current_alerts()) != self._image_alerts_key:
            self._async_start_refresh()
        super()._handle_coordinator_update()

    @callback
    def _async_start_refresh(self) -> asyncio.Task[None]:
        """Start a rebuild unless one is already running; return it either way."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self.hass.async_create_task(self._async_refresh())
        return self._refresh_task

    async def _async_refresh(self) -> None:
        """Rebuild the cached JPEG, again if the avisos changed mid-build."""
        while True:
            alerts = self._current_alerts()
            key = _alerts_key(alerts)
            try:
                image = await self._async_build_image(alerts)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Error building %s image: %s", self.entity_id, err, exc_info=True)
                return
            if not image:
                return
            self._image = image
            self._image_alerts_key = key
            if _alerts_key(self._current_alerts()) == key:
                return

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the cached JPEG, waiting for a rebuild if it lacks current avisos.

        Normally this is already warm from the background refresh. If it's
        the very first call, or the avisos changed since the last frame,
        wait (briefly) for the rebuild rather than return a stale image.
        """
        if self._image is None or self._image_alerts_key != _alerts_key(self._current_alerts()):
            task = self._async_start_refresh()
            try:
                await asyncio.wait_for(asyncio.shield(task), _FRESH_IMAGE_WAIT)
            except TimeoutError:
                _LOGGER.debug("%s rebuild still running, serving the previous frame", self.entity_id)
        return self._image


class SMNRadarCamera(_SMNVideoMixin, _SMNAlertsImageCamera):
    """Precipitation radar animation with SMN's active alert zones outlined.

    Like the satellite cameras: a GIF as the camera image and an MP4 for
    the dashboard card (see _SMNVideoMixin). Built in the background (see
    _SMNAlertsImageCamera) — the fetch (well over a hundred tiles) takes a
    few seconds, enough to blow past the short timeout some notification
    services use when attaching a camera snapshot.

    The downloaded layers are kept, so a change in the avisos between
    scheduled refreshes only redraws the frames: quick enough for an
    automation that fires on a new aviso to get its polygon.
    """

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
        super().__init__(coordinator, config_entry)
        self.content_type = "image/gif"
        self._attr_unique_id = f"{config_entry.entry_id}_radar"
        self._latitude = latitude
        self._longitude = longitude
        self._layers: RadarLayers | None = None
        self._layers_fetched_at = 0.0

    async def _async_build_image(self, alerts: list[dict[str, Any]]) -> bytes | None:
        # A minute of slack, so the scheduled refresh always fetches again.
        if self._layers is None or time.monotonic() - self._layers_fetched_at >= RADAR_UPDATE_INTERVAL - 60:
            self._layers = await fetch_radar_layers(
                async_get_clientsession(self.hass), self._latitude, self._longitude
            )
            self._layers_fetched_at = time.monotonic()
        data = self.coordinator.data
        frames = await self.hass.async_add_executor_job(
            render_radar_frames,
            self._layers,
            self._latitude,
            self._longitude,
            alerts,
            data.current_weather_data if data else None,
            data.hourly_forecast if data else None,
        )
        image = await self.hass.async_add_executor_job(encode_gif, frames)
        # The GIF is what a snapshot waits for; the MP4 follows on its own.
        self.hass.async_create_task(self._async_encode_video(frames))
        return image


class _SMNSatelliteAnimationCamera(
    _SMNVideoMixin, CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], Camera
):
    """Animated satellite view: a GIF as the camera image, plus an MP4 (see _SMNVideoMixin).

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
        latitude: float,
        longitude: float,
        force_infrared: bool,
    ) -> None:
        super().__init__(coordinator)
        Camera.__init__(self)
        self.content_type = "image/gif"
        self._attr_translation_key = kind
        self._attr_unique_id = f"{config_entry.entry_id}_{kind}"
        self._config_entry = config_entry
        self._latitude = latitude
        self._longitude = longitude
        self._force_infrared = force_infrared
        self._image: bytes | None = None
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
                self._image = await self.hass.async_add_executor_job(encode_gif, frames)
                video = await self.hass.async_add_executor_job(encode_mp4, frames)
                if video:
                    self.video = video
                    self._video_built_at = dt_util.utcnow()
                self.async_write_ha_state()
            elif not self._image:
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
        if self._image is None and not self._refreshing:
            await self._async_refresh()
        return self._image


class SMNSatelliteVideoView(HomeAssistantView):
    """Serves a satellite camera's MP4, at the URL its `entity_picture` holds.

    Accepts the camera's own rotating access token (the `?token=` that
    entity_picture carries, like /api/camera_proxy does), or regular Home
    Assistant auth: a bearer token, or a path signed with auth/sign_path.
    """

    url = f"/api/{DOMAIN}/video/{{entity_id}}"
    name = f"api:{DOMAIN}:video"
    requires_auth = False

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        camera = request.app[KEY_HASS].data.get(_VIDEOS_KEY, {}).get(entity_id)
        if camera is None:
            return web.Response(status=404)
        if not request[KEY_AUTHENTICATED] and request.query.get("token") not in camera.access_tokens:
            return web.Response(status=401)
        if not camera.video:
            return web.Response(status=404)
        return web.Response(
            body=camera.video,
            content_type="video/mp4",
            headers={"Cache-Control": "no-store"},
        )


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


class SMNCountryAlertsCamera(_SMNAlertsImageCamera):
    """Map of Argentina with every active short-term warning polygon (SMN's own geometry).

    Static JPEG on an OpenStreetMap basemap, rebuilt in the background like
    the radar camera. Uses the nationwide warning list, not the per-location
    one, so it shows warnings anywhere in the country.
    """

    _attr_translation_key = "country_alerts"
    _attr_attribution = "Avisos: SMN · Mapa: © OpenStreetMap contributors"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        name: str,
        latitude: float,
        longitude: float,
    ) -> None:
        super().__init__(coordinator, config_entry)
        self._attr_unique_id = f"{config_entry.entry_id}_country_alerts"
        self._pin = (latitude, longitude)

    async def _async_build_image(self, alerts: list[dict[str, Any]]) -> bytes | None:
        return await build_country_alerts_jpeg(
            async_get_clientsession(self.hass), alerts, self._pin
        )
