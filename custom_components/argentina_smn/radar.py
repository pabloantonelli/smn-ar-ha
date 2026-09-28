"""Animated precipitation radar built from RainViewer's public tile API.

Not sourced from SMN: see the note in const.py for why. This module fetches
the last few radar frames as a small tile mosaic around a lat/lon and
stitches them into an animated GIF, which a camera entity can then serve
directly (browsers animate GIFs shown via <img>, which is how HA's camera
proxy renders a still image today).
"""
from __future__ import annotations

import io
import logging
import math
import os
from typing import Any

import aiohttp
import async_timeout

_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")

from .const import (
    BASEMAP_TILE_URL_TEMPLATE,
    BASEMAP_USER_AGENT,
    RADAR_COLOR_SCHEME,
    RADAR_FRAME_COUNT,
    RADAR_FRAME_DURATION_MS,
    RADAR_TILE_GRID,
    RADAR_TILE_SIZE,
    RADAR_ZOOM,
    RAINVIEWER_INDEX_URL,
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


async def _fetch_frame_paths(session: aiohttp.ClientSession) -> tuple[str, list[str]]:
    """Return (tile_host, [frame_path, ...]) for the most recent frames."""
    async with async_timeout.timeout(10):
        response = await session.get(RAINVIEWER_INDEX_URL)
        response.raise_for_status()
        data = await response.json()

    host = data["host"]
    past = data.get("radar", {}).get("past", [])
    frames = [f["path"] for f in past[-RADAR_FRAME_COUNT:]]
    return host, frames


async def _fetch_tile(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str] | None = None
):
    """Fetch a single tile image, returning a transparent tile on failure."""
    from PIL import Image

    try:
        async with async_timeout.timeout(10):
            resp = await session.get(url, headers=headers)
            resp.raise_for_status()
            tile_bytes = await resp.read()
        return Image.open(io.BytesIO(tile_bytes)).convert("RGBA")
    except (aiohttp.ClientError, OSError) as err:
        _LOGGER.debug("Error fetching tile %s: %s", url, err)
        return Image.new("RGBA", (RADAR_TILE_SIZE, RADAR_TILE_SIZE), (0, 0, 0, 0))


async def _fetch_basemap_mosaic(
    session: aiohttp.ClientSession, center_x: int, center_y: int
):
    """Fetch and stitch the basemap tile grid (fetched once, reused per frame)."""
    from PIL import Image

    half = RADAR_TILE_GRID // 2
    mosaic = Image.new(
        "RGBA",
        (RADAR_TILE_SIZE * RADAR_TILE_GRID, RADAR_TILE_SIZE * RADAR_TILE_GRID),
    )

    for dx in range(-half, half + 1):
        for dy in range(-half, half + 1):
            x, y = center_x + dx, center_y + dy
            url = BASEMAP_TILE_URL_TEMPLATE.format(z=RADAR_ZOOM, x=x, y=y)
            tile_img = await _fetch_tile(
                session, url, headers={"User-Agent": BASEMAP_USER_AGENT}
            )
            mosaic.paste(
                tile_img, ((dx + half) * RADAR_TILE_SIZE, (dy + half) * RADAR_TILE_SIZE)
            )

    return mosaic


async def _fetch_radar_mosaic(
    session: aiohttp.ClientSession,
    host: str,
    frame_path: str,
    center_x: int,
    center_y: int,
    basemap: Any,
):
    """Fetch the radar tile grid for a single frame, composited over the basemap."""
    from PIL import Image

    half = RADAR_TILE_GRID // 2
    frame = basemap.copy()

    for dx in range(-half, half + 1):
        for dy in range(-half, half + 1):
            x, y = center_x + dx, center_y + dy
            url = (
                f"{host}{frame_path}/{RADAR_TILE_SIZE}/{RADAR_ZOOM}/{x}/{y}/"
                f"{RADAR_COLOR_SCHEME}/1_1.png"
            )
            tile_img = await _fetch_tile(session, url)
            frame.alpha_composite(
                tile_img, ((dx + half) * RADAR_TILE_SIZE, (dy + half) * RADAR_TILE_SIZE)
            )

    return frame


async def build_animated_radar_gif(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    alerts: list[dict[str, Any]] | None = None,
) -> bytes | None:
    """Build an animated GIF of the last few radar frames around lat/lon.

    `alerts` should be the *nationwide* avisos a muy corto plazo list (SMN's
    own, with their "geometry" field) — not the per-location one. This
    function filters it down to whatever overlaps the visible map itself
    (see filter_alerts_in_view), which is more useful here than SMN's
    per-location exact-point filtering: a station can be textually "in" an
    affected zone without its exact coordinate falling inside the drawn
    polygon, leaving the per-location list empty even when the map clearly
    shows an active alert nearby.
    """
    try:
        host, frame_paths = await _fetch_frame_paths(session)
    except (aiohttp.ClientError, KeyError) as err:
        _LOGGER.warning("Error fetching RainViewer frame index: %s", err)
        host, frame_paths = None, []

    center_x, center_y = _deg2tile(latitude, longitude, RADAR_ZOOM)

    basemap = await _fetch_basemap_mosaic(session, center_x, center_y)

    frames = []
    if host and frame_paths:
        for frame_path in frame_paths:
            frame = await _fetch_radar_mosaic(
                session, host, frame_path, center_x, center_y, basemap
            )
            frames.append(frame)
    else:
        # No radar frames (RainViewer unavailable) — still show the basemap
        # plus alert polygons, a single still frame beats nothing.
        frames.append(basemap.copy())

    visible_alerts = filter_alerts_in_view(alerts, center_x, center_y) if alerts else []
    if visible_alerts:
        for frame in frames:
            _draw_alert_polygons(frame, visible_alerts, center_x, center_y)
            _draw_alert_caption(frame, visible_alerts)

    if not frames:
        return None

    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=RADAR_FRAME_DURATION_MS,
        loop=0,
        disposal=2,
    )
    return buffer.getvalue()
