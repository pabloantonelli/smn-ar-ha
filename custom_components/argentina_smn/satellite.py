"""Satellite imagery snapshot built from NASA GIBS' GOES-East tiles.

See const.py for why GIBS instead of NOAA STAR's own CDN. This mirrors
radar.py's mosaic-building approach (same Web Mercator tile math, same
concurrent-fetch pattern), just against a different tile source, and adds
an optional "cloud motion" arrow estimated from two consecutive frames.
"""
from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import async_timeout

from .boundaries import draw_province_outline, get_outline_rings
from .const import (
    GIBS_LAYER_GEOCOLOR,
    GIBS_LAYER_INFRARED,
    GIBS_MATRIX_SET_GEOCOLOR,
    GIBS_MATRIX_SET_INFRARED,
    GIBS_TILE_URL_TEMPLATE,
    SATELLITE_ANIMATION_FRAMES,
    SATELLITE_TILE_GRID,
    SATELLITE_TILE_SIZE,
    SATELLITE_ZOOM,
)
from .radar import _deg2pixel, _deg2tile

_LOGGER = logging.getLogger(__name__)

# GIBS publishes frames on a 10-minute grid, but with variable processing
# lag (sometimes a couple minutes, sometimes 30+) — GetCapabilities showed
# gaps and a lag that isn't a fixed number of slots. So rather than assume
# "now rounded down minus N slots" is available (it 404s often enough to
# matter), the actual latest timestamp is resolved by probing.
_FRAME_INTERVAL = timedelta(minutes=10)
_MAX_LOOKBACK_SLOTS = 12  # up to 2h back before giving up on finding a frame


def _layer_for(is_daytime: bool) -> tuple[str, str]:
    """Return (layer, matrix_set) — GeoColor by day, clean IR by night.

    GeoColor is true-color imagery, so it's just a black frame after dark.
    Band13 clean infrared shows cloud-top temperature instead, which works
    the same day or night.
    """
    if is_daytime:
        return GIBS_LAYER_GEOCOLOR, GIBS_MATRIX_SET_GEOCOLOR
    return GIBS_LAYER_INFRARED, GIBS_MATRIX_SET_INFRARED


async def _resolve_latest_frame_time(
    session: aiohttp.ClientSession, layer: str, matrix_set: str
) -> datetime | None:
    """Find the newest 10-min-aligned timestamp GIBS actually has for `layer`.

    GIBS' publish lag isn't a fixed number of slots (GetCapabilities shows
    it varies from a couple minutes to 30+), so instead of guessing an
    offset from wall-clock time, this asks GIBS directly via its "default"
    time keyword (documented shortcut for "latest available"), fetching
    just the lightweight top-level tile (z0/0/0) to read back which
    timestamp actually served it, via the response's own bookkeeping.
    GIBS doesn't echo the resolved time in headers, so instead this probes
    candidate 10-min slots newest-first and uses the first one that
    doesn't 404 — capped at _MAX_LOOKBACK_SLOTS.
    """
    now = datetime.now(timezone.utc)
    aligned = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)
    for i in range(_MAX_LOOKBACK_SLOTS):
        candidate = aligned - i * _FRAME_INTERVAL
        url = GIBS_TILE_URL_TEMPLATE.format(
            layer=layer,
            time=candidate.strftime("%Y-%m-%dT%H:%M:%SZ"),
            matrix_set=matrix_set,
            z=0,
            x=0,
            y=0,
        )
        try:
            async with async_timeout.timeout(6):
                resp = await session.get(url)
                if resp.status == 200:
                    return candidate
        except (aiohttp.ClientError, asyncio.TimeoutError):
            continue
    return None


def _recent_frame_times(latest: datetime, count: int) -> list[datetime]:
    """Return `count` 10-min-aligned UTC timestamps ending at `latest`, oldest first."""
    times = [latest - i * _FRAME_INTERVAL for i in range(count)]
    times.reverse()
    return times


async def _fetch_tile(
    session: aiohttp.ClientSession, url: str
):
    """Fetch a single tile image, or None if GIBS doesn't have it (yet).

    Unlike radar.py's/the basemap's _fetch_tile, a missing GIBS tile isn't
    silently swapped for a transparent placeholder here — a timestamp with
    even one missing tile (GIBS sometimes publishes a frame's tiles
    incrementally, a few seconds apart) needs to be treated as incomplete
    and skipped by the caller, not stitched in as a black square.
    """
    from PIL import Image

    try:
        async with async_timeout.timeout(6):
            resp = await session.get(url)
            resp.raise_for_status()
            tile_bytes = await resp.read()
        return Image.open(io.BytesIO(tile_bytes)).convert("RGBA")
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Error fetching GIBS tile %s: %s", url, err)
        return None


async def _fetch_mosaic(
    session: aiohttp.ClientSession,
    layer: str,
    matrix_set: str,
    when: datetime,
    center_x: int,
    center_y: int,
) -> tuple[Any, bool]:
    """Fetch and stitch a tile grid for one GIBS frame timestamp.

    Returns (mosaic, complete) — `complete` is False if any tile in the
    grid was missing, so callers can fall back to an earlier timestamp
    instead of showing a mosaic with black holes in it.
    """
    from PIL import Image

    half = SATELLITE_TILE_GRID // 2
    mosaic = Image.new(
        "RGBA",
        (SATELLITE_TILE_SIZE * SATELLITE_TILE_GRID, SATELLITE_TILE_SIZE * SATELLITE_TILE_GRID),
    )

    time_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    positions = [(dx, dy) for dx in range(-half, half + 1) for dy in range(-half, half + 1)]
    urls = [
        GIBS_TILE_URL_TEMPLATE.format(
            layer=layer,
            time=time_str,
            matrix_set=matrix_set,
            z=SATELLITE_ZOOM,
            x=center_x + dx,
            y=center_y + dy,
        )
        for dx, dy in positions
    ]
    tiles = await asyncio.gather(*(_fetch_tile(session, url) for url in urls))
    complete = all(tile is not None for tile in tiles)
    for (dx, dy), tile_img in zip(positions, tiles):
        if tile_img is None:
            tile_img = Image.new("RGBA", (SATELLITE_TILE_SIZE, SATELLITE_TILE_SIZE), (0, 0, 0, 0))
        mosaic.paste(tile_img, ((dx + half) * SATELLITE_TILE_SIZE, (dy + half) * SATELLITE_TILE_SIZE))
    return mosaic, complete


async def _fetch_complete_mosaic(
    session: aiohttp.ClientSession,
    layer: str,
    matrix_set: str,
    start_time: datetime,
    center_x: int,
    center_y: int,
    max_attempts: int = 4,
):
    """Fetch a mosaic at start_time, stepping back a frame at a time until complete.

    Handles GIBS occasionally serving a partially-published timestamp (see
    _fetch_mosaic). Falls back through up to `max_attempts` earlier 10-min
    frames; if none come back complete, returns the last (incomplete) one
    fetched rather than nothing, since a slightly stale/partial frame beats
    an empty camera.
    """
    mosaic, complete, when = None, False, start_time
    for i in range(max_attempts):
        when = start_time - i * _FRAME_INTERVAL
        mosaic, complete = await _fetch_mosaic(session, layer, matrix_set, when, center_x, center_y)
        if complete:
            return mosaic, when
    _LOGGER.debug(
        "No complete GIBS mosaic found for %s within %d attempts before %s, using partial frame",
        layer, max_attempts, start_time,
    )
    return mosaic, when


def _estimate_motion_vector(frame_a: Any, frame_b: Any) -> tuple[float, float] | None:
    """Estimate the dominant pixel shift from frame_a to frame_b via phase correlation.

    This is a coarse visual approximation of cloud-pattern displacement
    between two mosaics of the same area (~10-20 min apart) — it reflects
    what moved across the image, distorted by the Web Mercator projection,
    not an actual measured wind vector. Good enough for "clouds are
    drifting this way on the screen", not for anything quantitative.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    gray_a = np.asarray(frame_a.convert("L"), dtype=np.float64)
    gray_b = np.asarray(frame_b.convert("L"), dtype=np.float64)
    if gray_a.shape != gray_b.shape:
        return None

    window = np.outer(np.hanning(gray_a.shape[0]), np.hanning(gray_a.shape[1]))
    fa = np.fft.fft2(gray_a * window)
    fb = np.fft.fft2(gray_b * window)
    cross_power = fa * np.conj(fb)
    magnitude = np.abs(cross_power)
    magnitude[magnitude == 0] = 1e-10
    cross_power /= magnitude
    correlation = np.fft.ifft2(cross_power).real

    peak_y, peak_x = np.unravel_index(np.argmax(correlation), correlation.shape)
    height, width = correlation.shape
    if peak_y > height // 2:
        peak_y -= height
    if peak_x > width // 2:
        peak_x -= width

    # A near-zero peak means no reliable dominant shift was found (e.g. a
    # mostly featureless clear-sky frame) — better to draw nothing than a
    # meaningless arrow.
    if abs(peak_x) < 1 and abs(peak_y) < 1:
        return None
    return float(peak_x), float(peak_y)


def _draw_motion_arrow(frame: Any, vector: tuple[float, float]) -> None:
    """Draw an arrow near the bottom-right showing the estimated cloud drift."""
    from PIL import ImageDraw
    import math

    dx, dy = vector
    magnitude = math.hypot(dx, dy)
    if magnitude == 0:
        return
    # Normalize to a fixed on-screen arrow length regardless of the raw
    # pixel shift measured (which depends on the time gap between frames).
    length = 40
    ux, uy = dx / magnitude, dy / magnitude

    margin = 60
    cx, cy = frame.width - margin, frame.height - margin
    x0, y0 = cx - ux * length / 2, cy - uy * length / 2
    x1, y1 = cx + ux * length / 2, cy + uy * length / 2

    draw = ImageDraw.Draw(frame, "RGBA")
    draw.ellipse(
        [cx - margin + 10, cy - margin + 10, cx + margin - 10, cy + margin - 10],
        fill=(0, 0, 0, 110),
    )
    draw.line([(x0, y0), (x1, y1)], fill=(255, 220, 40, 255), width=4)
    # Arrowhead.
    head_size = 10
    angle = math.atan2(uy, ux)
    for side in (1, -1):
        hx = x1 - head_size * math.cos(angle - side * math.pi / 6)
        hy = y1 - head_size * math.sin(angle - side * math.pi / 6)
        draw.line([(x1, y1), (hx, hy)], fill=(255, 220, 40, 255), width=4)


def _draw_outline(frame: Any, province: str | None, center_x: int, center_y: int) -> None:
    """Draw the configured province's outline (or all of Argentina as a fallback).

    Uses radar.py's _deg2pixel — safe to share since SATELLITE_TILE_SIZE
    equals RADAR_TILE_SIZE (both 256px), which is what that projection
    hardcodes internally.
    """
    rings = get_outline_rings(province)
    if not rings:
        return
    half = SATELLITE_TILE_GRID // 2
    origin_x = (center_x - half) * SATELLITE_TILE_SIZE
    origin_y = (center_y - half) * SATELLITE_TILE_SIZE
    draw_province_outline(
        frame, rings, lambda lat, lon: _deg2pixel(lat, lon, SATELLITE_ZOOM), origin_x, origin_y
    )


async def build_satellite_snapshot_jpeg(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    is_daytime: bool,
    with_motion_arrow: bool = True,
    province: str | None = None,
) -> bytes | None:
    """Build a single static JPEG: latest GIBS satellite frame for the area.

    When `with_motion_arrow` is set, also fetches the previous frame (one
    extra mosaic fetch) purely to estimate and draw a drift arrow — see
    _estimate_motion_vector's docstring for what that vector does and
    doesn't mean.
    """
    layer, matrix_set = _layer_for(is_daytime)
    center_x, center_y = _deg2tile(latitude, longitude, SATELLITE_ZOOM)
    latest_time = await _resolve_latest_frame_time(session, layer, matrix_set)
    if latest_time is None:
        _LOGGER.warning("No recent GIBS frame found for layer %s", layer)
        return None
    latest, resolved_time = await _fetch_complete_mosaic(
        session, layer, matrix_set, latest_time, center_x, center_y
    )
    frame = latest.copy()

    if with_motion_arrow:
        previous, _ = await _fetch_complete_mosaic(
            session, layer, matrix_set, resolved_time - _FRAME_INTERVAL, center_x, center_y
        )
        vector = _estimate_motion_vector(previous, latest)
        if vector:
            _draw_motion_arrow(frame, vector)

    _draw_outline(frame, province, center_x, center_y)

    buffer = io.BytesIO()
    frame.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def build_satellite_animation_gif(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    is_daytime: bool,
    province: str | None = None,
) -> bytes | None:
    """Build an animated GIF of the last SATELLITE_ANIMATION_FRAMES GIBS frames.

    Intended to be shared as a URL (this integration's satellite animation
    camera entity, whose entity_picture is a normal HA signed URL), not
    attached as a photo — several notification integrations mis-handle a
    non-JPEG camera attachment, see radar.py's docstring.
    """
    layer, matrix_set = _layer_for(is_daytime)
    center_x, center_y = _deg2tile(latitude, longitude, SATELLITE_ZOOM)
    latest_time = await _resolve_latest_frame_time(session, layer, matrix_set)
    if latest_time is None:
        _LOGGER.warning("No recent GIBS frame found for layer %s", layer)
        return None
    times = _recent_frame_times(latest_time, SATELLITE_ANIMATION_FRAMES)

    results = await asyncio.gather(
        *(_fetch_mosaic(session, layer, matrix_set, when, center_x, center_y) for when in times)
    )
    # A frame with any missing tile is dropped rather than shown with black
    # holes in it — see _fetch_mosaic's docstring. Occasionally losing one
    # of SATELLITE_ANIMATION_FRAMES beats a visibly broken animation.
    kept = [mosaic for mosaic, complete in results if complete]
    for mosaic in kept:
        _draw_outline(mosaic, province, center_x, center_y)
    frames = [mosaic.convert("RGB") for mosaic in kept]
    if not frames:
        return None

    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=400,
        loop=0,
    )
    return buffer.getvalue()
