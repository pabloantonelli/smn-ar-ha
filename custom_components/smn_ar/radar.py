"""Precipitation radar animation built from RainViewer's public tile API.

Not sourced from SMN: see the note in const.py for why. This module fetches
the last RADAR_ANIMATION_FRAMES radar frames as a small tile mosaic around a
lat/lon, with the infrared satellite's cold cloud tops underneath (RainViewer's
Argentina coverage is thin, so it often misses storms the satellite sees),
and draws SMN's own active alert zones and a short weather/forecast caption
on top. camera.py encodes the frames like the satellite cameras: a GIF as
the camera image and an MP4 for the dashboard card.

Fetching (fetch_radar_layers) and drawing (render_radar_frames) are split
so a change in the avisos only redraws the frames, without fetching again.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import async_timeout

_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")

from .boundaries import draw_region_overlay, get_country_rings
from .const import (
    BASEMAP_TILE_URL_TEMPLATE,
    BASEMAP_USER_AGENT,
    CONDITION_ID_MAP,
    CONDITION_LABELS_ES,
    GIBS_MAX_ZOOM_INFRARED,
    RADAR_ANIMATION_FRAMES,
    RADAR_COLOR_SCHEME,
    RADAR_TILE_GRID,
    RADAR_TILE_SIZE,
    RADAR_ZOOM,
    RAINVIEWER_INDEX_URL,
    RAINVIEWER_MAX_ZOOM,
)

_LOGGER = logging.getLogger(__name__)


def _deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Convert lat/lon to slippy-map tile x/y at the given zoom (Web Mercator)."""
    lat_rad = math.radians(lat)
    n = 2.0**zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
    )
    return x, y


def _deg2pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Convert lat/lon to fractional world-pixel coordinates at the given zoom."""
    lat_rad = math.radians(lat)
    n = 2.0**zoom
    px = (lon + 180.0) / 360.0 * n * RADAR_TILE_SIZE
    py = (
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi)
        / 2.0
        * n
        * RADAR_TILE_SIZE
    )
    return px, py


def _tile2deg(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Convert slippy-map tile x/y to the lat/lon of its NW corner."""
    n = 2.0**zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    lat = math.degrees(lat_rad)
    return lat, lon


def _viewport_bounds(
    center_x: int, center_y: int
) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon) currently visible on the mosaic."""
    half = RADAR_TILE_GRID // 2
    lat_nw, lon_nw = _tile2deg(center_x - half, center_y - half, RADAR_ZOOM)
    lat_se, lon_se = _tile2deg(center_x + half + 1, center_y + half + 1, RADAR_ZOOM)
    return min(lat_se, lat_nw), min(lon_nw, lon_se), max(lat_se, lat_nw), max(lon_nw, lon_se)


def filter_alerts_in_view(
    alerts: list[dict[str, Any]], center_x: int, center_y: int
) -> list[dict[str, Any]]:
    """Keep only alerts whose zone polygon overlaps the visible map area.

    SMN's own per-location warning/shortterm filtering is a strict
    point-in-polygon test against one exact coordinate, which is often
    empty even when a relevant alert's zone clearly covers the map the
    camera is showing (a station can be textually "in" an affected
    province/city per the zones list without its exact point falling
    inside the drawn polygon). Filtering by viewport overlap instead shows
    anything actually visible on the map, which is what matters for a
    picture — pass in the *nationwide* alerts list, not the per-location one.
    """
    min_lat, min_lon, max_lat, max_lon = _viewport_bounds(center_x, center_y)
    visible = []
    for alert in alerts:
        coordinates = (alert.get("geometry") or {}).get("coordinates") or []
        lats = [lat for ring in coordinates for _, lat in ring]
        lons = [lon for ring in coordinates for lon, _ in ring]
        if not lats or not lons:
            continue
        overlaps = (
            min(lats) <= max_lat
            and max(lats) >= min_lat
            and min(lons) <= max_lon
            and max(lons) >= min_lon
        )
        if overlaps:
            visible.append(alert)
    return visible


def _draw_alert_polygons(
    frame: Any, alerts: list[dict[str, Any]], center_x: int, center_y: int
) -> None:
    """Draw SMN's own avisos-a-muy-corto-plazo alert zone polygons on the frame.

    Uses the "geometry" field already returned by warning/shortterm — not an
    external data source, just visualizing data this integration already
    fetches from SMN. This is what actually matters most (where the active
    alert zone is), and unlike third-party radar mosaics it's always
    accurate for Argentina since it comes straight from SMN.
    """
    from PIL import ImageDraw

    half = RADAR_TILE_GRID // 2
    # World-pixel coordinate of this mosaic's top-left corner.
    origin_x = (center_x - half) * RADAR_TILE_SIZE
    origin_y = (center_y - half) * RADAR_TILE_SIZE

    draw = ImageDraw.Draw(frame, "RGBA")

    for alert in alerts:
        geometry = alert.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        for ring in coordinates:
            points = []
            for lon, lat in ring:
                px, py = _deg2pixel(lat, lon, RADAR_ZOOM)
                points.append((px - origin_x, py - origin_y))
            if len(points) >= 3:
                draw.polygon(points, outline=(220, 20, 20, 255), width=3)
                centroid_x = sum(p[0] for p in points) / len(points)
                centroid_y = sum(p[1] for p in points) / len(points)
                _draw_storm_icon(draw, centroid_x, centroid_y)


def _draw_location_pin(
    frame: Any, latitude: float, longitude: float, center_x: int, center_y: int
) -> None:
    """Mark the exact configured lat/lon with a small pin.

    No label — the forecast panel already shows temperature/condition at
    the top of this frame. Drawn at the *exact* point rather than assumed
    to be the frame's center: the center tile is only an approximation of
    where the configured coordinate actually falls within it.
    """
    from PIL import ImageDraw

    half = RADAR_TILE_GRID // 2
    origin_x = (center_x - half) * RADAR_TILE_SIZE
    origin_y = (center_y - half) * RADAR_TILE_SIZE
    px, py = _deg2pixel(latitude, longitude, RADAR_ZOOM)
    x, y = px - origin_x, py - origin_y

    draw = ImageDraw.Draw(frame, "RGBA")
    radius = 3.5
    draw.ellipse(
        [(x - radius, y - radius), (x + radius, y + radius)],
        fill=(255, 220, 60, 255),
        outline=(30, 30, 30, 220),
        width=1,
    )


def _draw_storm_icon(draw: Any, cx: float, cy: float, size: int = 22) -> None:
    """Draw a small storm-cloud-with-lightning marker at (cx, cy)."""
    half = size / 2
    cloud_color = (255, 255, 255, 230)
    outline_color = (60, 60, 60, 230)
    bolt_color = (255, 199, 44, 255)

    # Cloud: three overlapping ellipses + a rounded base.
    draw.ellipse(
        [cx - half, cy - half * 0.2, cx - half * 0.15, cy + half * 0.55],
        fill=cloud_color, outline=outline_color,
    )
    draw.ellipse(
        [cx - half * 0.35, cy - half * 0.75, cx + half * 0.55, cy + half * 0.35],
        fill=cloud_color, outline=outline_color,
    )
    draw.ellipse(
        [cx + half * 0.1, cy - half * 0.35, cx + half, cy + half * 0.55],
        fill=cloud_color, outline=outline_color,
    )

    # Lightning bolt.
    bolt = [
        (cx + half * 0.05, cy - half * 0.05),
        (cx - half * 0.25, cy + half * 0.75),
        (cx - half * 0.05, cy + half * 0.75),
        (cx - half * 0.3, cy + half * 1.35),
        (cx + half * 0.3, cy + half * 0.45),
        (cx + half * 0.05, cy + half * 0.45),
    ]
    draw.polygon(bolt, fill=bolt_color, outline=outline_color)


def _wrap_text(text: str, font: Any, draw: Any, max_width: int) -> list[str]:
    """Word-wrap text to fit max_width, using the given font."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _draw_alert_caption(frame: Any, alerts: list[dict[str, Any]], bottom: int = 0) -> None:
    """Draw the SMN aviso's own text (title + validity) as a caption banner.

    `bottom` leaves that many pixels free under it (the timeline bar).

    Same text already shown by the short_term_summary sensor, burned into
    the image itself so it reads like SMN's/other providers' map popups
    without needing to open the entity's attributes.
    """
    from PIL import ImageDraw, ImageFont

    if not alerts:
        return

    alert = alerts[0]
    title = (alert.get("title") or "").strip().rstrip(".").capitalize()
    end_time = None
    end_date = alert.get("end_date")
    if end_date and "T" in end_date:
        end_time = end_date.split("T", 1)[1][:5]

    caption = title
    if end_time:
        caption += f" (válido hasta las {end_time})"

    try:
        # Pillow's ImageFont.load_default() bitmap font lacks glyphs for
        # Spanish accents (á, é, í, ó, ú, ñ) — they render as tofu boxes.
        # DejaVuSans is bundled with this integration specifically for that.
        font = ImageFont.truetype(_FONT_PATH, size=16)
    except OSError:
        _LOGGER.warning("Bundled font not found at %s, falling back to default", _FONT_PATH)
        try:
            font = ImageFont.load_default(size=16)
        except TypeError:
            font = ImageFont.load_default()

    draw = ImageDraw.Draw(frame, "RGBA")
    padding = 8
    max_text_width = frame.width - 2 * padding
    lines = _wrap_text(caption, font, draw, max_text_width)

    line_height = font.size + 4 if hasattr(font, "size") else 18
    banner_height = len(lines) * line_height + 2 * padding

    bottom_y = frame.height - bottom
    draw.rectangle(
        [(0, bottom_y - banner_height), (frame.width, bottom_y)],
        fill=(0, 0, 0, 170),
    )
    for i, line in enumerate(lines):
        draw.text(
            (padding, bottom_y - banner_height + padding + i * line_height),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )


def _condition_label_es(weather: dict[str, Any] | None) -> str | None:
    """Map an SMN weather condition dict to a short Spanish label.

    Reuses CONDITION_ID_MAP (SMN id -> HA condition constant) and then
    CONDITION_LABELS_ES (HA condition constant -> Spanish text) so this
    stays consistent with the weather entity's own condition, without
    depending on HA core's Lokalise-managed translations (unavailable to
    a Pillow-drawn image anyway).
    """
    if not isinstance(weather, dict):
        return None
    ha_condition = CONDITION_ID_MAP.get(weather.get("id"))
    if not ha_condition:
        return None
    return CONDITION_LABELS_ES.get(ha_condition)


def _draw_forecast_panel(
    frame: Any,
    current_weather: dict[str, Any] | None,
    hourly_forecast: list[dict[str, Any]] | None,
) -> int:
    """Draw a top banner with current temp/condition and the next few periods; return its height.

    current_weather / hourly_forecast are coordinator.data's own fields
    (already fetched for the weather entity and sensors) — this just
    burns a summary of the same data into the radar image so it's useful
    as a single self-contained snapshot, e.g. attached to a chat message.
    """
    from PIL import ImageDraw, ImageFont

    if not current_weather and not hourly_forecast:
        return 0

    try:
        font_big = ImageFont.truetype(_FONT_PATH, size=22)
        font_small = ImageFont.truetype(_FONT_PATH, size=15)
    except OSError:
        _LOGGER.warning("Bundled font not found at %s, falling back to default", _FONT_PATH)
        try:
            font_big = ImageFont.load_default(size=22)
            font_small = ImageFont.load_default(size=15)
        except TypeError:
            font_big = font_small = ImageFont.load_default()

    lines: list[str] = []

    temperature = (current_weather or {}).get("temperature")
    condition_label = _condition_label_es((current_weather or {}).get("weather"))
    if temperature is not None:
        header = f"{temperature:.0f}°C"
        if condition_label:
            header += f" · {condition_label}"
        lines.append(header)
    elif condition_label:
        lines.append(condition_label)

    now = datetime.now()
    upcoming = []
    for period in hourly_forecast or []:
        period_dt = period.get("datetime")
        if not period_dt:
            continue
        try:
            parsed = datetime.fromisoformat(period_dt)
        except ValueError:
            continue
        if parsed >= now:
            upcoming.append((parsed, period))
    upcoming.sort(key=lambda item: item[0])

    forecast_parts = []
    for parsed, period in upcoming[:4]:
        temp = period.get("temperature")
        rain_range = period.get("rain_prob_range") or []
        rain = max(rain_range) if rain_range else None
        part = parsed.strftime("%H:%M")
        if temp is not None:
            part += f" {temp:.0f}°"
        if rain is not None:
            part += f" {rain:.0f}% lluvia"
        forecast_parts.append(part)
    if forecast_parts:
        lines.append("  ·  ".join(forecast_parts))

    if not lines:
        return 0

    draw = ImageDraw.Draw(frame, "RGBA")
    padding = 8
    line_height_big = font_big.size + 4 if hasattr(font_big, "size") else 26
    line_height_small = font_small.size + 4 if hasattr(font_small, "size") else 19
    banner_height = line_height_big + (len(lines) - 1) * line_height_small + 2 * padding

    draw.rectangle([(0, 0), (frame.width, banner_height)], fill=(0, 0, 0, 170))
    y = padding
    for i, line in enumerate(lines):
        font = font_big if i == 0 else font_small
        draw.text((padding, y), line, font=font, fill=(255, 255, 255, 255))
        y += line_height_big if i == 0 else line_height_small
    return banner_height


# Sampled from GIBS' Band13 palette, coldest last (see satellite.py).
_CLOUD_TOPS_LEGEND_COLORS = [(60, 210, 90), (235, 220, 40), (235, 50, 40)]


def _draw_cloud_tops_legend(frame: Any, top: int) -> None:
    """Small key in the top-right corner, just under the forecast banner.

    The cold cloud tops layer is satellite data, not precipitation: without
    a label it would read as more radar.
    """
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(_FONT_PATH, size=15)
    except OSError:
        font = ImageFont.load_default()

    label = "Nubes de tormenta (satélite)"
    draw = ImageDraw.Draw(frame, "RGBA")
    padding, swatch, gap = 8, 14, 8
    text_width = draw.textlength(label, font=font)
    box_width = len(_CLOUD_TOPS_LEGEND_COLORS) * swatch + gap + text_width + 2 * padding
    box_height = swatch + 2 * padding
    x0, y0 = frame.width - box_width, top
    draw.rectangle([(x0, y0), (frame.width, y0 + box_height)], fill=(0, 0, 0, 170))
    for i, color in enumerate(_CLOUD_TOPS_LEGEND_COLORS):
        sx = x0 + padding + i * swatch
        draw.rectangle([(sx, y0 + padding), (sx + swatch, y0 + padding + swatch)], fill=color)
    draw.text(
        (x0 + padding + len(_CLOUD_TOPS_LEGEND_COLORS) * swatch + gap, y0 + padding - 2),
        label,
        font=font,
        fill=(255, 255, 255, 255),
    )


async def _fetch_frame_paths(
    session: aiohttp.ClientSession,
) -> tuple[str, list[tuple[datetime, str]]]:
    """Return (tile_host, [(UTC time, frame_path), ...]) for the most recent frames, oldest first."""
    async with async_timeout.timeout(10):
        response = await session.get(RAINVIEWER_INDEX_URL)
        response.raise_for_status()
        data = await response.json()

    host = data["host"]
    past = data.get("radar", {}).get("past", [])
    frames = [
        (datetime.fromtimestamp(f["time"], timezone.utc), f["path"])
        for f in past[-RADAR_ANIMATION_FRAMES:]
    ]
    return host, frames


_MAX_CONCURRENT_TILE_FETCHES = 16
_TILE_SEMAPHORE: asyncio.Semaphore | None = None


def _tile_semaphore() -> asyncio.Semaphore:
    """Cap on in-flight radar/basemap tile requests.

    An animation fetches well over a hundred tiles at once; queued in the
    connection pool, their timeout would run out before they were even
    sent (see satellite.py's _tile_semaphore). Separate from that one so
    the radar camera never waits behind a satellite animation.
    """
    global _TILE_SEMAPHORE  # noqa: PLW0603
    if _TILE_SEMAPHORE is None:
        _TILE_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_TILE_FETCHES)
    return _TILE_SEMAPHORE


async def _fetch_tile(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str] | None = None
):
    """Fetch a single tile image, returning a transparent tile on failure."""
    from PIL import Image

    try:
        async with _tile_semaphore(), async_timeout.timeout(6):
            resp = await session.get(url, headers=headers)
            resp.raise_for_status()
            tile_bytes = await resp.read()
        return Image.open(io.BytesIO(tile_bytes)).convert("RGBA")
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Error fetching tile %s: %s", url, err)
        return Image.new("RGBA", (RADAR_TILE_SIZE, RADAR_TILE_SIZE), (0, 0, 0, 0))


async def _fetch_basemap_mosaic(
    session: aiohttp.ClientSession, center_x: int, center_y: int
):
    """Fetch and stitch the basemap tile grid (fetched once, reused per frame).

    All tiles are fetched concurrently — with a 5x5 grid that's 25 requests,
    which sequentially (the original implementation) could take well past
    typical notification/snapshot timeouts. In parallel it's bounded by the
    single slowest tile instead of the sum of all of them.
    """
    from PIL import Image

    half = RADAR_TILE_GRID // 2
    mosaic = Image.new(
        "RGBA",
        (RADAR_TILE_SIZE * RADAR_TILE_GRID, RADAR_TILE_SIZE * RADAR_TILE_GRID),
    )

    positions = [(dx, dy) for dx in range(-half, half + 1) for dy in range(-half, half + 1)]
    urls = [
        BASEMAP_TILE_URL_TEMPLATE.format(z=RADAR_ZOOM, x=center_x + dx, y=center_y + dy)
        for dx, dy in positions
    ]
    tiles = await asyncio.gather(
        *(_fetch_tile(session, url, headers={"User-Agent": BASEMAP_USER_AGENT}) for url in urls)
    )

    for (dx, dy), tile_img in zip(positions, tiles):
        mosaic.paste(
            tile_img, ((dx + half) * RADAR_TILE_SIZE, (dy + half) * RADAR_TILE_SIZE)
        )

    return mosaic


async def _fetch_radar_layer(
    session: aiohttp.ClientSession,
    host: str,
    frame_path: str,
    center_x: int,
    center_y: int,
):
    """Fetch RainViewer's radar tiles for the mosaic's area, at RainViewer's own zoom.

    RainViewer's radar tiles top out at RAINVIEWER_MAX_ZOOM (zoom 8+ returns
    a "Zoom Level Not Supported" placeholder image) — verified directly
    against their tile server. When RADAR_ZOOM is higher than that (for a
    more detailed basemap), the radar tiles covering the same area are
    fetched at RAINVIEWER_MAX_ZOOM instead, and render_radar_frames
    scales them up to fit, so the map itself can still be more zoomed in
    even though the radar data's own resolution is capped by RainViewer.
    """
    from PIL import Image

    half = RADAR_TILE_GRID // 2
    target_size = RADAR_TILE_SIZE * RADAR_TILE_GRID
    origin_x = (center_x - half) * RADAR_TILE_SIZE
    origin_y = (center_y - half) * RADAR_TILE_SIZE

    if RAINVIEWER_MAX_ZOOM >= RADAR_ZOOM:
        # No scaling needed, fetch directly at RADAR_ZOOM.
        layer = Image.new("RGBA", (target_size, target_size))
        positions = [(dx, dy) for dx in range(-half, half + 1) for dy in range(-half, half + 1)]
        urls = [
            f"{host}{frame_path}/{RADAR_TILE_SIZE}/{RADAR_ZOOM}/{center_x + dx}/{center_y + dy}/"
            f"{RADAR_COLOR_SCHEME}/1_1.png"
            for dx, dy in positions
        ]
        tiles = await asyncio.gather(*(_fetch_tile(session, url) for url in urls))
        for (dx, dy), tile_img in zip(positions, tiles):
            layer.paste(
                tile_img, ((dx + half) * RADAR_TILE_SIZE, (dy + half) * RADAR_TILE_SIZE)
            )
        return layer

    scale = 2.0 ** (RAINVIEWER_MAX_ZOOM - RADAR_ZOOM)
    origin_x_r = origin_x * scale
    origin_y_r = origin_y * scale
    size_r = target_size * scale

    tile_x_start = int(origin_x_r // RADAR_TILE_SIZE)
    tile_x_end = int((origin_x_r + size_r) // RADAR_TILE_SIZE)
    tile_y_start = int(origin_y_r // RADAR_TILE_SIZE)
    tile_y_end = int((origin_y_r + size_r) // RADAR_TILE_SIZE)

    raw = Image.new(
        "RGBA",
        (
            (tile_x_end - tile_x_start + 1) * RADAR_TILE_SIZE,
            (tile_y_end - tile_y_start + 1) * RADAR_TILE_SIZE,
        ),
    )
    tile_positions = [
        (x, y)
        for x in range(tile_x_start, tile_x_end + 1)
        for y in range(tile_y_start, tile_y_end + 1)
    ]
    urls = [
        f"{host}{frame_path}/{RADAR_TILE_SIZE}/{RAINVIEWER_MAX_ZOOM}/{x}/{y}/"
        f"{RADAR_COLOR_SCHEME}/1_1.png"
        for x, y in tile_positions
    ]
    tiles = await asyncio.gather(*(_fetch_tile(session, url) for url in urls))
    for (x, y), tile_img in zip(tile_positions, tiles):
        raw.paste(
            tile_img,
            ((x - tile_x_start) * RADAR_TILE_SIZE, (y - tile_y_start) * RADAR_TILE_SIZE),
        )

    crop_left = round(origin_x_r - tile_x_start * RADAR_TILE_SIZE)
    crop_top = round(origin_y_r - tile_y_start * RADAR_TILE_SIZE)
    return raw.crop(
        (crop_left, crop_top, crop_left + round(size_r), crop_top + round(size_r))
    )


@dataclass
class RadarFrame:
    """One animation frame's data, before anything is drawn on it."""

    when: datetime  # UTC
    radar: Any | None  # RainViewer layer at its own zoom (see _fetch_radar_layer)
    cloud_tops: Any | None  # infrared raster (see satellite.cold_cloud_tops_layer)
    # SINARAME images per radar for this frame, newest first: its own and
    # the previous couple of slots, since some radars interleave scans
    # that are pure artifacts (render_radar_frames draws the first good one).
    sinarame: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class RadarLayers:
    """Everything fetch_radar_layers downloads for one animation."""

    center_x: int
    center_y: int
    basemap: Any
    frames: list[RadarFrame]  # oldest first
    sinarame_radars: list[str] = field(default_factory=list)  # the ones with images


# How far back a frame looks for a usable SINARAME image of each radar.
_SINARAME_FRAME_FALLBACK = 2  # 10-min slots


async def fetch_radar_layers(
    session: aiohttp.ClientSession, latitude: float, longitude: float
) -> RadarLayers:
    """Download the basemap once, plus each frame's radar and infrared layers.

    With SINARAME radars covering the map (see nowcast.py), the frames are
    theirs: the last RADAR_ANIMATION_FRAMES 10-min slots up to their
    newest image, ~25 min behind. RainViewer and the infrared stay under
    them, matched by time. Without any SINARAME image, frame times are
    RainViewer's (10 min apart) as before; and if RainViewer is down too,
    the animation still shows the infrared over the last
    RADAR_ANIMATION_FRAMES 10-minute slots.
    """
    # Imported here: satellite.py and nowcast.py import this module.
    from .nowcast import fetch_sinarame_frames, sinarame_radars_in_view
    from .satellite import fetch_cold_cloud_tops_rasters

    center_x, center_y = _deg2tile(latitude, longitude, RADAR_ZOOM)
    sinarame_radars = sinarame_radars_in_view(*_viewport_bounds(center_x, center_y))

    async def rainviewer_paths() -> tuple[str | None, list[tuple[datetime, str]]]:
        try:
            return await _fetch_frame_paths(session)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as err:
            _LOGGER.warning("Error fetching RainViewer frame index: %s", err)
            return None, []

    (host, paths), *sinarame_images = await asyncio.gather(
        rainviewer_paths(),
        *(
            fetch_sinarame_frames(session, radar_id, RADAR_ANIMATION_FRAMES + _SINARAME_FRAME_FALLBACK + 4)
            for radar_id in sinarame_radars
        ),
    )
    sinarame_by_radar = {
        radar_id: images for radar_id, images in zip(sinarame_radars, sinarame_images) if images
    }
    newest_sinarame = max((max(images) for images in sinarame_by_radar.values()), default=None)
    if newest_sinarame is not None:
        times = [newest_sinarame - timedelta(minutes=10 * i) for i in reversed(range(RADAR_ANIMATION_FRAMES))]
    elif paths:
        times = [when for when, _ in paths]
    else:
        now = datetime.now(timezone.utc)
        aligned = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
        times = [aligned - timedelta(minutes=10 * i) for i in reversed(range(RADAR_ANIMATION_FRAMES))]
    paths = [(when, path) for when, path in paths if when in times]

    half = RADAR_TILE_GRID // 2
    basemap, cloud_tops, radar_layers = await asyncio.gather(
        _fetch_basemap_mosaic(session, center_x, center_y),
        fetch_cold_cloud_tops_rasters(
            session,
            (center_x - half) * RADAR_TILE_SIZE,
            (center_y - half) * RADAR_TILE_SIZE,
            RADAR_TILE_SIZE * RADAR_TILE_GRID,
            RADAR_ZOOM,
            times,
        ),
        asyncio.gather(
            *(_fetch_radar_layer(session, host, path, center_x, center_y) for _, path in paths)
        ),
    )
    radar_by_time = {when: layer for (when, _), layer in zip(paths, radar_layers)}

    def sinarame_for(when: datetime) -> dict[str, list[Any]]:
        candidates = {}
        for radar_id, images in sinarame_by_radar.items():
            recent = [
                images[when - timedelta(minutes=10 * i)]
                for i in range(_SINARAME_FRAME_FALLBACK + 1)
                if when - timedelta(minutes=10 * i) in images
            ]
            if recent:
                candidates[radar_id] = recent
        return candidates

    return RadarLayers(
        center_x,
        center_y,
        basemap,
        [
            RadarFrame(when, radar_by_time.get(when), clouds, sinarame_for(when))
            for when, clouds in zip(times, cloud_tops)
        ],
        [radar_id for radar_id in sinarame_radars if radar_id in sinarame_by_radar],
    )


# SINARAME's ~1 km pixels, scaled up ~4x onto the map: blurred after
# scaling, so cells have soft edges instead of blocks.
_SINARAME_BLUR_PX = 4
_COVERAGE_FEATHER_PX = 40


def _sinarame_layers(
    layers: RadarLayers, size: int, origin_x: float, origin_y: float
) -> tuple[list[Any | None], Any | None]:
    """(each frame's SINARAME layer or None, the radars' coverage on the map or None).

    Per radar: each frame's image filtered (see nowcast.sinarame_dbz),
    picking the fullest of its recent candidates (some radars interleave
    a sparse scan); then its sparse scans and static clutter dropped
    across the animation (nowcast.clean_radar_frames). The radars are
    combined taking the strongest echo where they overlap (the usual
    composite: nearest-on-top would cut rain with straight edges),
    smoothed, and colored like RainViewer.

    A frame without its infrared can't be filtered against interference:
    it gets no SINARAME layer. The coverage (0-1, feathered) is the
    radars' 240 km circles, where the infrared isn't drawn.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    from .nowcast import _RADAR_WEAK_DBZ, SINARAME_RADARS, clean_radar_frames, colorize_dbz, sinarame_dbz

    scale = 2.0 ** (GIBS_MAX_ZOOM_INFRARED - RADAR_ZOOM)
    infrared_origin = (origin_x * scale, origin_y * scale)
    total = len(layers.frames)
    composites: list[Any | None] = [None] * total
    coverage = None
    for radar_id in layers.sinarame_radars:
        decoded: dict[tuple[int, int], Any] = {}
        per_frame: list[Any | None] = []
        for frame_data in layers.frames:
            best = None
            infrared = frame_data.cloud_tops
            for image in frame_data.sinarame.get(radar_id, []) if infrared is not None else []:
                key = (id(image), id(infrared))
                if key not in decoded:
                    decoded[key] = sinarame_dbz(radar_id, image, infrared, infrared_origin)
                dbz = decoded[key]
                if dbz is not None and (best is None or (dbz >= _RADAR_WEAK_DBZ).sum() > (best >= _RADAR_WEAK_DBZ).sum()):
                    best = dbz
            # A copy: clean_radar_frames edits in place, and frames can share one.
            per_frame.append(None if best is None else best.copy())
        present = [i for i, dbz in enumerate(per_frame) if dbz is not None]
        if not present:
            continue
        keep = {present[i] for i in clean_radar_frames([per_frame[i] for i in present], _RADAR_WEAK_DBZ)}

        _, south, west, north, east = SINARAME_RADARS[radar_id]
        x0, y0 = _deg2pixel(north, west, RADAR_ZOOM)
        x1, y1 = _deg2pixel(south, east, RADAR_ZOOM)
        width, height = round(x1 - x0), round(y1 - y0)
        left, top = round(x0 - origin_x), round(y0 - origin_y)
        mx0, my0 = max(left, 0), max(top, 0)
        mx1, my1 = min(left + width, size), min(top + height, size)
        if mx0 >= mx1 or my0 >= my1:
            continue
        if coverage is None:
            coverage = Image.new("L", (size, size))
        radius = width / 2  # the square is drawn around the radar's 240 km circle
        cx, cy = left + width / 2, top + height / 2
        ImageDraw.Draw(coverage).ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=255)
        # A frame left without this radar holds its previous one for a
        # couple of slots: an empty map would read as "no rain".
        shown, last = [], None
        for i in range(total):
            if i in keep:
                last = i
            shown.append(last if last is not None and i - last <= _SINARAME_FRAME_FALLBACK else None)
        resized_by_frame: dict[int, Any] = {}
        for i, source in enumerate(shown):
            if source is None:
                continue
            if source not in resized_by_frame:
                resized_by_frame[source] = np.asarray(
                    Image.fromarray(per_frame[source], mode="F").resize((width, height), Image.BILINEAR)
                )
            resized = resized_by_frame[source]
            if composites[i] is None:
                composites[i] = np.zeros((size, size), dtype=np.float32)
            np.maximum(
                composites[i][my0:my1, mx0:mx1],
                resized[my0 - top : my1 - top, mx0 - left : mx1 - left],
                out=composites[i][my0:my1, mx0:mx1],
            )

    images: list[Any | None] = []
    for composite in composites:
        if composite is None:
            images.append(None)
            continue
        # Blurred as 8-bit (PIL won't blur float images): 1/3 dBZ steps.
        smooth = Image.fromarray((np.clip(composite, 0, 85) * 3).astype(np.uint8))
        smooth = smooth.filter(ImageFilter.GaussianBlur(_SINARAME_BLUR_PX))
        images.append(colorize_dbz(np.asarray(smooth).astype(np.float32) / 3, soft=True))
    if coverage is not None:
        coverage = coverage.filter(ImageFilter.GaussianBlur(_COVERAGE_FEATHER_PX))
    return images, coverage


def render_radar_frames(
    layers: RadarLayers,
    latitude: float,
    longitude: float,
    alerts: list[dict[str, Any]] | None = None,
    current_weather: dict[str, Any] | None = None,
    hourly_forecast: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Draw every animation frame (oldest first): layers, then overlays on top.

    CPU only, run it in an executor. Encode with satellite.encode_gif /
    encode_mp4, like the satellite cameras.

    `alerts` should be the *nationwide* avisos a muy corto plazo list (SMN's
    own, with their "geometry" field) — not the per-location one. This
    function filters it down to whatever overlaps the visible map itself
    (see filter_alerts_in_view), which is more useful here than SMN's
    per-location exact-point filtering: a station can be textually "in" an
    affected zone without its exact coordinate falling inside the drawn
    polygon, leaving the per-location list empty even when the map clearly
    shows an active alert nearby. The avisos are the current ones, drawn on
    every frame.

    `current_weather` / `hourly_forecast` are coordinator.data's own fields,
    burned into a caption banner so the image is useful standalone.
    """
    from PIL import Image, ImageChops

    from .satellite import (
        _caption_bar_height,
        _draw_caption,
        _format_frame_caption,
        cold_cloud_tops_layer,
    )

    center_x, center_y = layers.center_x, layers.center_y
    size = layers.basemap.width
    half = RADAR_TILE_GRID // 2
    origin_x = (center_x - half) * RADAR_TILE_SIZE
    origin_y = (center_y - half) * RADAR_TILE_SIZE

    sinarame_layers, coverage = _sinarame_layers(layers, size, origin_x, origin_y)

    # The newest frames can share one infrared raster (GIBS lags behind
    # RainViewer): process each distinct one once. Only outside the
    # SINARAME radars' reach: both layers on top of each other just
    # muddle the map, and where there's radar, it's the one that sees rain.
    processed: dict[int, Any] = {}
    cloud_layers = []
    for frame_data in layers.frames:
        raster = frame_data.cloud_tops
        if raster is not None and id(raster) not in processed:
            clouds = cold_cloud_tops_layer(raster, size)
            if clouds is not None and coverage is not None:
                alpha = ImageChops.multiply(clouds.getchannel("A"), ImageChops.invert(coverage))
                clouds.putalpha(alpha)
                if alpha.getbbox() is None:
                    clouds = None
            processed[id(raster)] = clouds
        cloud_layers.append(processed.get(id(raster)) if raster is not None else None)
    has_cloud_tops = any(layer is not None for layer in cloud_layers)

    # Always the national outline here, not the province's: this camera is
    # zoomed in tight around one lat/lon (a fixed ~area, no zoom control),
    # so a province boundary would usually run off-frame or be
    # unrecognizable — the country outline reads better as a "where in
    # Argentina is this" reference at this scale. A province that's fully
    # visible at its own natural zoom belongs on a dedicated camera
    # instead (see satellite.py's region cameras).
    rings = get_country_rings()
    visible_alerts = filter_alerts_in_view(alerts, center_x, center_y) if alerts else []
    total = len(layers.frames)
    timeline_height = _caption_bar_height(size, total > 1)

    frames = []
    for i, (frame_data, clouds) in enumerate(zip(layers.frames, cloud_layers)):
        frame = layers.basemap.copy()
        # Cloud tops under the radar: where both show, the radar's actual
        # precipitation is the more precise of the two.
        if clouds is not None:
            frame.alpha_composite(clouds)
        if frame_data.radar is not None:
            radar = frame_data.radar
            if radar.size != frame.size:
                radar = radar.resize(frame.size, Image.NEAREST)
            frame.alpha_composite(radar)
        # SINARAME over RainViewer: in Argentina it's the one that sees the rain.
        sinarame = sinarame_layers[i]
        if sinarame is not None:
            frame.alpha_composite(sinarame)
        if rings:
            draw_region_overlay(
                frame, rings, lambda lat, lon: _deg2pixel(lat, lon, RADAR_ZOOM), origin_x, origin_y
            )
        if visible_alerts:
            _draw_alert_polygons(frame, visible_alerts, center_x, center_y)
            _draw_alert_caption(frame, visible_alerts, bottom=timeline_height)
        _draw_location_pin(frame, latitude, longitude, center_x, center_y)
        panel_height = _draw_forecast_panel(frame, current_weather, hourly_forecast)
        if has_cloud_tops:
            _draw_cloud_tops_legend(frame, panel_height)
        _draw_caption(
            frame, _format_frame_caption(frame_data.when), frame_index=i, total_frames=total
        )
        frames.append(frame.convert("RGB"))
    return frames
