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
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import async_timeout

from .boundaries import draw_region_overlay, get_bbox, get_country_rings
from .const import (
    GIBS_LAYER_GEOCOLOR,
    GIBS_LAYER_INFRARED,
    GIBS_MATRIX_SET_GEOCOLOR,
    GIBS_MATRIX_SET_INFRARED,
    GIBS_MAX_ZOOM_GEOCOLOR,
    GIBS_MAX_ZOOM_INFRARED,
    GIBS_TILE_URL_TEMPLATE,
    SATELLITE_ANIMATION_FRAMES,
    SATELLITE_REGION_TILE_GRID,
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


def _layer_for(is_daytime: bool, force_infrared: bool = False) -> tuple[str, str]:
    """Return (layer, matrix_set) — GeoColor by day, clean IR by night (or always, if forced).

    GeoColor is true-color imagery, so it's just a black frame after dark.
    Band13 clean infrared shows cloud-top temperature instead, which works
    the same day or night — `force_infrared` is for a camera that always
    wants that view (e.g. to see storm-top structure that reads better in
    IR than in daylight GeoColor), not just as the night fallback.
    """
    if force_infrared or not is_daytime:
        return GIBS_LAYER_INFRARED, GIBS_MATRIX_SET_INFRARED
    return GIBS_LAYER_GEOCOLOR, GIBS_MATRIX_SET_GEOCOLOR


def _max_zoom_for(is_daytime: bool, force_infrared: bool = False) -> int:
    if force_infrared or not is_daytime:
        return GIBS_MAX_ZOOM_INFRARED
    return GIBS_MAX_ZOOM_GEOCOLOR


def _zoom_for_bbox(
    min_lat: float,
    min_lon: float,
    max_lat: float,
    max_lon: float,
    tile_grid: int,
    max_zoom: int,
    fill_factor: float = 0.9,
) -> int:
    """Find the highest zoom level at which the bbox still fits the tile grid.

    Searches from max_zoom down to 0 and picks the first level where the
    bbox's projected pixel span fits within `tile_grid` tiles times
    `fill_factor`. A lower fill_factor (e.g. the default 0.9) leaves a
    margin so the outline isn't flush against the edge; a higher one
    (>1) deliberately lets the bbox exceed the grid — corners get
    cropped — in exchange for a noticeably tighter zoom.
    """
    budget = tile_grid * SATELLITE_TILE_SIZE * fill_factor
    for zoom in range(max_zoom, -1, -1):
        x0, y0 = _deg2pixel(max_lat, min_lon, zoom)
        x1, y1 = _deg2pixel(min_lat, max_lon, zoom)
        if abs(x1 - x0) <= budget and abs(y1 - y0) <= budget:
            return zoom
    return 0


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


# Argentina doesn't observe DST, so a fixed UTC-3 offset is always correct
# (unlike using the host's local time, which may not even be Argentina's).
_ARG_UTC_OFFSET = timedelta(hours=-3)
_CAPTION_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")

# Reference width every overlay size below is tuned against: the local
# fixed-zoom camera's frame (SATELLITE_TILE_GRID * SATELLITE_TILE_SIZE =
# 3*256 = 768px). The country/province cameras use a 6-tile grid (1536px)
# at that same tile size, so a fixed font/dot/swatch size that reads fine
# on the small camera ends up tiny on those — and _apply_extra_zoom's
# post-crop upscale doesn't help, since it resamples pixels, not overlays
# drawn after it. Scaling every overlay by the frame's actual width keeps
# text/dots/swatches a consistent *proportion* of the image across every
# camera, instead of a fixed pixel size that only looks right on one of
# them.
_REFERENCE_FRAME_WIDTH = 768


def _overlay_scale(frame: Any) -> float:
    """How much bigger/smaller `frame` is than the reference 768px frame."""
    return frame.width / _REFERENCE_FRAME_WIDTH


def _scaled(base: float, scale: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, base * scale))


def _load_font(size: int) -> Any:
    from PIL import ImageFont

    try:
        return ImageFont.truetype(_CAPTION_FONT_PATH, size=size)
    except OSError:
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()


def _format_frame_caption(when: datetime) -> str:
    """Format a frame's timestamp as "HH:MM · hace N min" (Argentina local time).

    The "hace N min" part is what a still image otherwise can't convey —
    GIBS' publish lag means "the latest frame" can be anywhere from ~10 to
    ~40 min old, and for an animation it's what turns a strip of frames
    into an actual timeline (same idea as Windy's scrubber labels).
    """
    local = when + _ARG_UTC_OFFSET
    minutes_ago = max(0, int((datetime.now(timezone.utc) - when).total_seconds() // 60))
    if minutes_ago == 0:
        relative = "recién"
    elif minutes_ago == 1:
        relative = "hace 1 min"
    else:
        relative = f"hace {minutes_ago} min"
    return f"{local.strftime('%H:%M')} · {relative}"


def _draw_caption(
    frame: Any,
    text: str,
    frame_index: int | None = None,
    total_frames: int | None = None,
) -> None:
    """Draw a timestamp banner across the bottom of the frame.

    A single still image just gets the timestamp text. An animation frame
    (when `frame_index`/`total_frames` are given) also gets a timeline bar
    below it — a connecting line with one tick per frame, the current one
    highlighted and labeled "N/total" — so the strip of frames reads as an
    actual scrubber while it plays, not just a changing clock in the
    corner. Every size here scales with the frame's own width (see
    _overlay_scale) so this looks the same proportionally whether it's
    drawn on the small local camera or the much bigger country/province
    mosaics.
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame, "RGBA")
    scale = _overlay_scale(frame)

    font_size = round(_scaled(22, scale, 20, 38))
    padding = round(_scaled(10, scale, 10, 18))
    font = _load_font(font_size)

    has_timeline = frame_index is not None and total_frames and total_frames > 1
    timeline_height = round(_scaled(22, scale, 20, 34)) if has_timeline else 0
    text_height = font_size
    bar_height = text_height + 2 * padding + timeline_height

    draw.rectangle(
        [(0, frame.height - bar_height), (frame.width, frame.height)], fill=(0, 0, 0, 180)
    )
    draw.text(
        (padding, frame.height - bar_height + padding - 2),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )

    if has_timeline:
        margin = round(_scaled(22, scale, 20, 40))
        line_y = frame.height - timeline_height / 2 + round(_scaled(3, scale, 2, 6))
        span = frame.width - 2 * margin
        step = span / (total_frames - 1) if total_frames > 1 else 0

        draw.line(
            [(margin, line_y), (frame.width - margin, line_y)],
            fill=(255, 255, 255, 110),
            width=max(1, round(_scaled(2, scale, 1, 3))),
        )

        tick_radius = _scaled(3.5, scale, 3, 6)
        current_radius = _scaled(6, scale, 5, 10)
        for i in range(total_frames):
            cx = margin + i * step
            if i == frame_index:
                draw.ellipse(
                    [
                        (cx - current_radius, line_y - current_radius),
                        (cx + current_radius, line_y + current_radius),
                    ],
                    fill=(255, 220, 60, 255),
                    outline=(0, 0, 0, 200),
                    width=max(1, round(scale)),
                )
            else:
                draw.ellipse(
                    [(cx - tick_radius, line_y - tick_radius), (cx + tick_radius, line_y + tick_radius)],
                    fill=(255, 255, 255, 170),
                )

        counter = f"{frame_index + 1}/{total_frames}"
        counter_font = _load_font(round(_scaled(16, scale, 15, 26)))
        counter_width = draw.textlength(counter, font=counter_font)
        draw.text(
            (frame.width - padding - counter_width, frame.height - bar_height + padding - 2),
            counter,
            font=counter_font,
            fill=(255, 220, 60, 255),
        )


_IR_LEGEND_STEPS = [
    ((60, 60, 65, 235), "Nubes altas"),
    ((40, 180, 220, 235), "Frío"),
    ((60, 210, 90, 235), "Más frío"),
    ((235, 220, 40, 235), "Muy frío"),
    ((235, 50, 40, 235), "Tormenta severa"),
]


def _draw_ir_legend(frame: Any) -> None:
    """Draw a small qualitative legend for the infrared color palette.

    GIBS' Band13 Clean Infrared tiles come pre-colored (not raw grayscale
    temperature data this code controls), and NASA doesn't publish the
    exact color-to-Kelvin breakpoints for this specific rendering — so
    this is a qualitative "what the colors mean" key (colder cloud tops
    read as more severe), not a precise numeric scale like a Kelvin bar
    would imply. Good enough to make the palette legible without
    overstating precision this code can't verify.
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame, "RGBA")
    scale = _overlay_scale(frame)

    font_size = round(_scaled(13, scale, 14, 26))
    font = _load_font(font_size)
    swatch = round(_scaled(14, scale, 15, 30))
    gap = round(_scaled(8, scale, 8, 16))
    padding = round(_scaled(10, scale, 10, 20))
    row_height = swatch + round(_scaled(6, scale, 6, 12))

    label_widths = [draw.textlength(label, font=font) for _, label in _IR_LEGEND_STEPS]
    box_width = swatch + gap + max(label_widths) + 2 * padding
    box_height = len(_IR_LEGEND_STEPS) * row_height + padding

    x0, y0 = frame.width - box_width, 0
    draw.rectangle([(x0, y0), (frame.width, y0 + box_height)], fill=(0, 0, 0, 165))
    for i, (color, label) in enumerate(_IR_LEGEND_STEPS):
        row_y = y0 + padding // 2 + i * row_height
        draw.rectangle(
            [(x0 + padding, row_y), (x0 + padding + swatch, row_y + swatch)],
            fill=color,
            outline=(255, 255, 255, 90),
        )
        draw.text(
            (x0 + padding + swatch + gap, row_y + (swatch - font_size) / 2 - 1),
            label,
            font=font,
            fill=(255, 255, 255, 255),
        )


def _draw_location_pin(
    frame: Any,
    latitude: float,
    longitude: float,
    zoom: int,
    origin_x: float,
    origin_y: float,
    current_weather: dict[str, Any] | None,
) -> None:
    """Mark the configured location with a pin, labeled with its current temperature.

    Drawn on every satellite view (local, country, province) so the
    configured point is identifiable on the image itself, not just
    implied by "this is roughly the area shown". `current_weather` is
    coordinator.data's own field (already fetched for the weather entity),
    same shape radar.py's caption uses — temperature-only here (not the
    fuller forecast strip radar.py draws), since these frames are already
    busier with the outline/legend/timeline overlays.
    """
    from PIL import ImageDraw

    px, py = _deg2pixel(latitude, longitude, zoom)
    x, y = px - origin_x, py - origin_y
    if not (-20 <= x <= frame.width + 20 and -20 <= y <= frame.height + 20):
        return  # Off-frame (e.g. a province view where the point falls outside it).

    draw = ImageDraw.Draw(frame, "RGBA")
    scale = _overlay_scale(frame)

    # Deliberately small relative to the frame — a marker that helps locate
    # the point without covering meaningful area of the image, especially
    # on an animation where it sits on every frame. Still scaled a little
    # with the frame so it isn't a sub-pixel speck on the big country view.
    radius = _scaled(3.5, scale, 3.5, 6)
    draw.ellipse(
        [(x - radius, y - radius), (x + radius, y + radius)],
        fill=(255, 220, 60, 255),
        outline=(20, 20, 20, 230),
        width=max(1, round(scale)),
    )

    temperature = (current_weather or {}).get("temperature")
    if temperature is None:
        return
    label = f"{temperature:.0f}°C"

    font_size = round(_scaled(13, scale, 13, 22))
    font = _load_font(font_size)
    pad_x, pad_y = round(_scaled(4, scale, 4, 7)), round(_scaled(2, scale, 2, 4))

    text_width = draw.textlength(label, font=font)
    text_height = font_size
    label_x = x + radius + round(_scaled(4, scale, 4, 7))
    label_y = y - text_height / 2

    box = [
        (label_x - pad_x, label_y - pad_y),
        (label_x + text_width + pad_x, label_y + text_height + pad_y),
    ]
    # If the label would run off the right edge, flip it to the pin's left.
    if box[1][0] > frame.width:
        label_x = x - radius - round(_scaled(4, scale, 4, 7)) - text_width
        box = [
            (label_x - pad_x, label_y - pad_y),
            (label_x + text_width + pad_x, label_y + text_height + pad_y),
        ]

    draw.rectangle(box, fill=(0, 0, 0, 180), outline=(255, 255, 255, 60))
    draw.text((label_x, label_y), label, font=font, fill=(255, 255, 255, 255))


def _local_origin(center_x: int, center_y: int) -> tuple[float, float]:
    """World-pixel origin (top-left) of the local fixed-zoom tile grid."""
    half = SATELLITE_TILE_GRID // 2
    return (center_x - half) * SATELLITE_TILE_SIZE, (center_y - half) * SATELLITE_TILE_SIZE


def _draw_outline(frame: Any, center_x: int, center_y: int) -> None:
    """Draw Argentina's national outline as a light location-context reference.

    Always the country outline, not the configured province's: this camera
    is zoomed in tight around one lat/lon with no zoom control, so a
    province boundary would usually run off-frame or be unrecognizable at
    this scale — see radar.py's build_radar_snapshot_jpeg for the same
    reasoning. A province that's fully visible at its own natural zoom
    belongs on build_region_snapshot_jpeg instead.

    Uses radar.py's _deg2pixel — safe to share since SATELLITE_TILE_SIZE
    equals RADAR_TILE_SIZE (both 256px), which is what that projection
    hardcodes internally.
    """
    rings = get_country_rings()
    if not rings:
        return
    half = SATELLITE_TILE_GRID // 2
    origin_x = (center_x - half) * SATELLITE_TILE_SIZE
    origin_y = (center_y - half) * SATELLITE_TILE_SIZE
    draw_region_overlay(
        frame, rings, lambda lat, lon: _deg2pixel(lat, lon, SATELLITE_ZOOM), origin_x, origin_y
    )


async def build_satellite_snapshot_jpeg(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    is_daytime: bool,
    with_motion_arrow: bool = True,
    force_infrared: bool = False,
    current_weather: dict[str, Any] | None = None,
) -> bytes | None:
    """Build a single static JPEG: latest GIBS satellite frame for the area.

    When `with_motion_arrow` is set, also fetches the previous frame (one
    extra mosaic fetch) purely to estimate and draw a drift arrow — see
    _estimate_motion_vector's docstring for what that vector does and
    doesn't mean. `force_infrared` always uses Band13 clean IR instead of
    the day/night-dependent default — for a camera that wants that view
    specifically, not just as the night fallback. `current_weather` (same
    shape as coordinator.data's field) labels the location pin's
    temperature, if given.
    """
    layer, matrix_set = _layer_for(is_daytime, force_infrared)
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

    _draw_outline(frame, center_x, center_y)
    _draw_location_pin(
        frame,
        latitude,
        longitude,
        SATELLITE_ZOOM,
        *_local_origin(center_x, center_y),
        current_weather,
    )
    if layer == GIBS_LAYER_INFRARED:
        _draw_ir_legend(frame)
    _draw_caption(frame, _format_frame_caption(resolved_time))

    buffer = io.BytesIO()
    frame.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def build_satellite_animation_gif(
    session: aiohttp.ClientSession,
    latitude: float,
    longitude: float,
    is_daytime: bool,
    force_infrared: bool = False,
    current_weather: dict[str, Any] | None = None,
) -> bytes | None:
    """Build an animated GIF of the last SATELLITE_ANIMATION_FRAMES GIBS frames.

    Intended to be shared as a URL (this integration's satellite animation
    camera entity, whose entity_picture is a normal HA signed URL), not
    attached as a photo — several notification integrations mis-handle a
    non-JPEG camera attachment, see radar.py's docstring.
    """
    layer, matrix_set = _layer_for(is_daytime, force_infrared)
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
    kept = [(mosaic, when) for (mosaic, complete), when in zip(results, times) if complete]
    total = len(kept)
    for i, (mosaic, when) in enumerate(kept):
        _draw_outline(mosaic, center_x, center_y)
        _draw_location_pin(
            mosaic,
            latitude,
            longitude,
            SATELLITE_ZOOM,
            *_local_origin(center_x, center_y),
            current_weather,
        )
        if layer == GIBS_LAYER_INFRARED:
            _draw_ir_legend(mosaic)
        _draw_caption(mosaic, _format_frame_caption(when), frame_index=i, total_frames=total)
    frames = [mosaic.convert("RGB") for mosaic, _when in kept]
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


async def _fetch_region_mosaic(
    session: aiohttp.ClientSession,
    layer: str,
    matrix_set: str,
    when: datetime,
    zoom: int,
    center_x: int,
    center_y: int,
) -> tuple[Any, bool]:
    """Same as _fetch_mosaic, but at an arbitrary zoom and SATELLITE_REGION_TILE_GRID size."""
    from PIL import Image

    half = SATELLITE_REGION_TILE_GRID // 2
    mosaic = Image.new(
        "RGBA",
        (
            SATELLITE_TILE_SIZE * SATELLITE_REGION_TILE_GRID,
            SATELLITE_TILE_SIZE * SATELLITE_REGION_TILE_GRID,
        ),
    )
    time_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    positions = [(dx, dy) for dx in range(-half, half + 1) for dy in range(-half, half + 1)]
    urls = [
        GIBS_TILE_URL_TEMPLATE.format(
            layer=layer,
            time=time_str,
            matrix_set=matrix_set,
            z=zoom,
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


async def _fetch_complete_region_mosaic(
    session: aiohttp.ClientSession,
    layer: str,
    matrix_set: str,
    start_time: datetime,
    zoom: int,
    center_x: int,
    center_y: int,
    max_attempts: int = 4,
):
    """Same fallback as _fetch_complete_mosaic, for the region (country/province) grid."""
    mosaic, complete, when = None, False, start_time
    for i in range(max_attempts):
        when = start_time - i * _FRAME_INTERVAL
        mosaic, complete = await _fetch_region_mosaic(
            session, layer, matrix_set, when, zoom, center_x, center_y
        )
        if complete:
            return mosaic, when
    _LOGGER.debug(
        "No complete GIBS region mosaic found for %s within %d attempts before %s, using partial frame",
        layer, max_attempts, start_time,
    )
    return mosaic, when


def _region_center_and_zoom(
    rings: list[list[tuple[float, float]]], is_daytime: bool, force_infrared: bool = False
) -> tuple[str, str, int, int, int, int, int]:
    """Resolve (layer, matrix_set, zoom, center_x, center_y, origin_x, origin_y) for `rings`.

    Always uses the same 0.9 fit margin — GIBS' own tile resolution caps
    out at GIBS_MAX_ZOOM_GEOCOLOR/INFRARED (7/6), which a small-ish
    province typically already hits well before this margin matters; going
    tighter than the data's native resolution happens afterwards, as a
    post-fetch crop to the shape's own bbox — see _center_on_shape.
    """
    layer, matrix_set = _layer_for(is_daytime, force_infrared)
    max_zoom = _max_zoom_for(is_daytime, force_infrared)
    min_lat, min_lon, max_lat, max_lon = get_bbox(rings)
    zoom = _zoom_for_bbox(min_lat, min_lon, max_lat, max_lon, SATELLITE_REGION_TILE_GRID, max_zoom)
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2
    center_x, center_y = _deg2tile(center_lat, center_lon, zoom)
    half = SATELLITE_REGION_TILE_GRID // 2
    origin_x = (center_x - half) * SATELLITE_TILE_SIZE
    origin_y = (center_y - half) * SATELLITE_TILE_SIZE
    return layer, matrix_set, zoom, center_x, center_y, origin_x, origin_y


def _rings_pixel_bbox(
    rings: list[list[tuple[float, float]]],
    deg2pixel: Any,
    origin_x: float,
    origin_y: float,
) -> tuple[float, float, float, float]:
    """Pixel-space (x0, y0, x1, y1) bounding box of `rings` on the mosaic."""
    xs: list[float] = []
    ys: list[float] = []
    for ring in rings:
        for lon, lat in ring:
            px, py = deg2pixel(lat, lon)
            xs.append(px - origin_x)
            ys.append(py - origin_y)
    return min(xs), min(ys), max(xs), max(ys)


def _center_on_shape(
    frame: Any,
    bbox: tuple[float, float, float, float],
    margin_ratio: float = 0.08,
) -> Any:
    """Crop `frame` tightly around `bbox` (a shape's own pixel bbox) and
    upscale back to the original frame size.

    The tile grid used to fetch region mosaics is always square, but a
    province's actual shape rarely is (Córdoba, for instance, is tall and
    narrow) — fitting a square grid to a non-square shape leaves large
    empty margins on whichever axis has slack, and the shape isn't
    necessarily centered in them either. Cropping to the shape's own pixel
    bbox (plus a small margin so the outline isn't flush against the edge)
    and scaling back up fixes both at once: it's now centered by
    definition, and fills the frame as much as the shape's own aspect
    ratio allows — replaces the old uniform `_apply_extra_zoom` approach
    for region cameras, which cropped a fixed ratio around the frame's
    center regardless of the shape's actual footprint in it.
    """
    from PIL import Image

    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    mx, my = w * margin_ratio, h * margin_ratio
    x0, y0 = max(0, x0 - mx), max(0, y0 - my)
    x1, y1 = min(frame.width, x1 + mx), min(frame.height, y1 + my)
    if x1 <= x0 or y1 <= y0:
        return frame
    cropped = frame.crop((x0, y0, x1, y1))

    # Scale back up so the output uses as much of the original frame's
    # resolution as the crop's own aspect ratio allows — matching the
    # *larger* dimension to the original frame (not the smaller, and
    # uncapped) so a tall/narrow province like Córdoba still fills most of
    # a square frame at full resolution instead of coming out small. An
    # earlier version capped this at 2x to avoid upscaling blur, but that
    # produced visibly small/blocky images for anything whose bbox was a
    # small fraction of the fetched tile grid (routinely the case for the
    # infrared layer, whose native GIBS resolution is already one zoom
    # level coarser than GeoColor's) — a bit of LANCZOS softness beats a
    # genuinely low-resolution image the UI then stretches anyway.
    scale = max(frame.width / cropped.width, frame.height / cropped.height)
    if scale <= 1.01:
        return cropped
    new_size = (round(cropped.width * scale), round(cropped.height * scale))
    return cropped.resize(new_size, Image.LANCZOS)


async def build_region_snapshot_jpeg(
    session: aiohttp.ClientSession,
    rings: list[list[tuple[float, float]]],
    is_daytime: bool,
    force_infrared: bool = False,
    pin: tuple[float, float] | None = None,
    current_weather: dict[str, Any] | None = None,
    subdivisions: list[list[tuple[float, float]]] | None = None,
) -> bytes | None:
    """Build a static JPEG covering a whole area (a province, or all of Argentina).

    Unlike build_satellite_snapshot_jpeg (fixed zoom around one lat/lon),
    the zoom level here is computed from `rings`' own bounding box so the
    whole area fits the frame — this is what lets a dedicated "Córdoba" or
    "Argentina" camera actually show the full province/country, which
    isn't possible at the local camera's fixed street-level-ish zoom. The
    result is then cropped tightly to the shape's own pixel bbox and
    centered (see _center_on_shape) — a province is rarely square, so
    fitting it into a square tile grid otherwise leaves it off-center with
    empty margins. Its own outline is drawn on top (see
    boundaries.draw_region_overlay), with the area outside it darkened
    heavily (not just dimmed) so the shape itself is unambiguously what
    the camera is "about". `pin` is the configured location's (latitude,
    longitude), marked with `current_weather`'s temperature — left out
    (None) if the point wouldn't fall inside this frame anyway.
    `subdivisions` (optional) draws internal borders inside `rings` —
    departments for a province camera, provinces for the country camera.
    """
    if not rings:
        return None
    layer, matrix_set, zoom, center_x, center_y, origin_x, origin_y = _region_center_and_zoom(
        rings, is_daytime, force_infrared
    )

    latest_time = await _resolve_latest_frame_time(session, layer, matrix_set)
    if latest_time is None:
        _LOGGER.warning("No recent GIBS frame found for layer %s", layer)
        return None

    mosaic, resolved_time = await _fetch_complete_region_mosaic(
        session, layer, matrix_set, latest_time, zoom, center_x, center_y
    )
    deg2pixel = lambda lat, lon: _deg2pixel(lat, lon, zoom)  # noqa: E731
    draw_region_overlay(
        mosaic,
        rings,
        deg2pixel,
        origin_x,
        origin_y,
        darken_alpha=220,
        subdivision_rings=subdivisions,
    )
    if pin:
        _draw_location_pin(mosaic, pin[0], pin[1], zoom, origin_x, origin_y, current_weather)
    bbox = _rings_pixel_bbox(rings, deg2pixel, origin_x, origin_y)
    mosaic = _center_on_shape(mosaic, bbox)
    if layer == GIBS_LAYER_INFRARED:
        _draw_ir_legend(mosaic)
    _draw_caption(mosaic, _format_frame_caption(resolved_time))

    buffer = io.BytesIO()
    mosaic.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def build_region_animation_gif(
    session: aiohttp.ClientSession,
    rings: list[list[tuple[float, float]]],
    is_daytime: bool,
    force_infrared: bool = False,
    pin: tuple[float, float] | None = None,
    current_weather: dict[str, Any] | None = None,
    subdivisions: list[list[tuple[float, float]]] | None = None,
) -> bytes | None:
    """Build an animated GIF of the last SATELLITE_ANIMATION_FRAMES frames for a whole area.

    Same idea as build_satellite_animation_gif, but zoomed to fit `rings`'
    bounding box (see build_region_snapshot_jpeg) instead of a fixed zoom
    around a lat/lon — used for the province animation camera. `subdivisions`
    is the same as in build_region_snapshot_jpeg (departments/provinces).
    """
    if not rings:
        return None
    layer, matrix_set, zoom, center_x, center_y, origin_x, origin_y = _region_center_and_zoom(
        rings, is_daytime, force_infrared
    )

    latest_time = await _resolve_latest_frame_time(session, layer, matrix_set)
    if latest_time is None:
        _LOGGER.warning("No recent GIBS frame found for layer %s", layer)
        return None
    times = _recent_frame_times(latest_time, SATELLITE_ANIMATION_FRAMES)

    results = await asyncio.gather(
        *(
            _fetch_region_mosaic(session, layer, matrix_set, when, zoom, center_x, center_y)
            for when in times
        )
    )
    kept = [(mosaic, when) for (mosaic, complete), when in zip(results, times) if complete]
    total = len(kept)
    deg2pixel = lambda lat, lon: _deg2pixel(lat, lon, zoom)  # noqa: E731
    bbox = _rings_pixel_bbox(rings, deg2pixel, origin_x, origin_y)
    zoomed: list[Any] = []
    for i, (mosaic, when) in enumerate(kept):
        draw_region_overlay(
            mosaic,
            rings,
            deg2pixel,
            origin_x,
            origin_y,
            darken_alpha=220,
            subdivision_rings=subdivisions,
        )
        if pin:
            _draw_location_pin(mosaic, pin[0], pin[1], zoom, origin_x, origin_y, current_weather)
        mosaic = _center_on_shape(mosaic, bbox)
        if layer == GIBS_LAYER_INFRARED:
            _draw_ir_legend(mosaic)
        _draw_caption(mosaic, _format_frame_caption(when), frame_index=i, total_frames=total)
        zoomed.append(mosaic)
    frames = [mosaic.convert("RGB") for mosaic in zoomed]
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
