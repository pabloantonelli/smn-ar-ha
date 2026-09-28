"""Precipitation radar snapshot built from RainViewer's public tile API.

Not sourced from SMN: see the note in const.py for why. This module fetches
the latest radar frame as a small tile mosaic around a lat/lon, composites
SMN's own active alert zones on top, and adds a short weather/forecast
caption — all baked into a single static JPEG.

This used to build an animated GIF (several frames stitched together), but
that broke attaching the camera as a message attachment in most
notification integrations (Telegram, WhatsApp-via-Baileys, etc.), which
assume a camera entity is a static photo the way HA's own default camera
content type (JPEG) implies. RainViewer's Argentina coverage is thin enough
that the animation rarely showed real movement anyway, so a static image
with more useful info (temperature, condition, next-hours forecast) is a
better trade.
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import os
from datetime import datetime
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


def _draw_alert_caption(frame: Any, alerts: list[dict[str, Any]]) -> None:
    """Draw the SMN aviso's own text (title + validity) as a caption banner.

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

    draw.rectangle(
        [(0, frame.height - banner_height), (frame.width, frame.height)],
        fill=(0, 0, 0, 170),
    )
    for i, line in enumerate(lines):
        draw.text(
            (padding, frame.height - banner_height + padding + i * line_height),
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
) -> None:
    """Draw a top banner with current temp/condition and the next few periods.

    current_weather / hourly_forecast are coordinator.data's own fields
    (already fetched for the weather entity and sensors) — this just
    burns a summary of the same data into the radar image so it's useful
    as a single self-contained snapshot, e.g. attached to a chat message.
    """
    from PIL import ImageDraw, ImageFont

    if not current_weather and not hourly_forecast:
        return

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
        return

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


async def _fetch_frame_paths(session: aiohttp.ClientSession) -> tuple[str, list[str]]:
    """Return (tile_host, [frame_path, ...]) for the most recent frames."""
    async with async_timeout.timeout(10):
        response = await session.get(RAINVIEWER_INDEX_URL)
        response.raise_for_status()
        data = await response.json()

    host = data["host"]
    past = data.get("radar", {}).get("past", [])
    frames = [f["path"] for f in past[-1:]]
    return host, frames


async def _fetch_tile(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str] | None = None
):
    """Fetch a single tile image, returning a transparent tile on failure."""
    from PIL import Image

    try:
        async with async_timeout.timeout(6):
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
    """Fetch RainViewer's radar tiles for the mosaic's area and scale to match it.

    RainViewer's radar tiles top out at RAINVIEWER_MAX_ZOOM (zoom 8+ returns
    a "Zoom Level Not Supported" placeholder image) — verified directly
    against their tile server. When RADAR_ZOOM is higher than that (for a
    more detailed basemap), the radar tiles covering the same area are
    fetched at RAINVIEWER_MAX_ZOOM instead and resized up to fit, so the
    map itself can still be more zoomed in even though the radar data's
    own resolution is capped by RainViewer.
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
    cropped = raw.crop(
        (crop_left, crop_top, crop_left + round(size_r), crop_top + round(size_r))
    )
    return cropped.resize((target_size, target_size), Image.NEAREST)


async def build_radar_snapshot_jpeg(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    alerts: list[dict[str, Any]] | None = None,
    current_weather: dict[str, Any] | None = None,
    hourly_forecast: list[dict[str, Any]] | None = None,
) -> bytes | None:
    """Build a single static JPEG: basemap + latest radar frame + overlays.

    A static JPEG (not an animated GIF) so it works as a message attachment
    in third-party notification integrations that assume a camera entity is
    a plain photo — see this module's docstring.

    `alerts` should be the *nationwide* avisos a muy corto plazo list (SMN's
    own, with their "geometry" field) — not the per-location one. This
    function filters it down to whatever overlaps the visible map itself
    (see filter_alerts_in_view), which is more useful here than SMN's
    per-location exact-point filtering: a station can be textually "in" an
    affected zone without its exact coordinate falling inside the drawn
    polygon, leaving the per-location list empty even when the map clearly
    shows an active alert nearby.

    `current_weather` / `hourly_forecast` are coordinator.data's own fields,
    burned into a caption banner so the image is useful standalone.
    """
    try:
        host, frame_paths = await _fetch_frame_paths(session)
    except (aiohttp.ClientError, KeyError) as err:
        _LOGGER.warning("Error fetching RainViewer frame index: %s", err)
        host, frame_paths = None, []

    center_x, center_y = _deg2tile(latitude, longitude, RADAR_ZOOM)

    basemap = await _fetch_basemap_mosaic(session, center_x, center_y)

    frame = basemap.copy()
    if host and frame_paths:
        layer = await _fetch_radar_layer(session, host, frame_paths[-1], center_x, center_y)
        frame.alpha_composite(layer)

    # Always the national outline here, not the province's: this camera is
    # zoomed in tight around one lat/lon (a fixed ~area, no zoom control),
    # so a province boundary would usually run off-frame or be
    # unrecognizable — the country outline reads better as a "where in
    # Argentina is this" reference at this scale. A province that's fully
    # visible at its own natural zoom belongs on a dedicated camera
    # instead (see satellite.py's region cameras).
    rings = get_country_rings()
    if rings:
        half = RADAR_TILE_GRID // 2
        origin_x = (center_x - half) * RADAR_TILE_SIZE
        origin_y = (center_y - half) * RADAR_TILE_SIZE
        draw_region_overlay(
            frame, rings, lambda lat, lon: _deg2pixel(lat, lon, RADAR_ZOOM), origin_x, origin_y
        )

    visible_alerts = filter_alerts_in_view(alerts, center_x, center_y) if alerts else []
    if visible_alerts:
        _draw_alert_polygons(frame, visible_alerts, center_x, center_y)
        _draw_alert_caption(frame, visible_alerts)

    _draw_location_pin(frame, latitude, longitude, center_x, center_y)
    _draw_forecast_panel(frame, current_weather, hourly_forecast)

    buffer = io.BytesIO()
    frame.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
