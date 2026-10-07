"""Animated satellite views (whole country / one province) from NASA GIBS' GOES-East tiles.

Same Web Mercator tile math as radar.py, against GIBS' public WMTS tiles
(see const.py for why GIBS). Each animation is rendered once and encoded as
a short looping MP4 (see encode_mp4), plus a GIF of the same frames for
the camera image (see encode_gif).
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

from .boundaries import draw_region_overlay, get_bbox
from .const import (
    GIBS_LAYER_GEOCOLOR,
    GIBS_LAYER_INFRARED,
    GIBS_MATRIX_SET_GEOCOLOR,
    GIBS_MATRIX_SET_INFRARED,
    GIBS_MAX_ZOOM_GEOCOLOR,
    GIBS_MAX_ZOOM_INFRARED,
    GIBS_TILE_URL_TEMPLATE,
    SATELLITE_ANIMATION_FRAMES,
    SATELLITE_ANIMATION_LOOKBACK_BUFFER,
    SATELLITE_TILE_SIZE,
)
from .radar import _deg2pixel, _fetch_tile as _fetch_tile_unqueued

_LOGGER = logging.getLogger(__name__)

# GIBS publishes frames on a 10-minute grid, but with variable processing
# lag (sometimes a couple minutes, sometimes 30+) — GetCapabilities showed
# gaps and a lag that isn't a fixed number of slots. So rather than assume
# "now rounded down minus N slots" is available (it 404s often enough to
# matter), the actual latest timestamp is resolved by probing.
_FRAME_INTERVAL = timedelta(minutes=10)
_MAX_LOOKBACK_SLOTS = 12  # up to 2h back before giving up on finding a frame


def _layer_for(force_infrared: bool = False) -> tuple[str, str]:
    """Return (layer, matrix_set): GeoColor, or Band13 clean infrared if forced.

    No automatic night fallback to infrared: GIBS' GeoColor product already
    blends in a night-time view (gray IR-based clouds over city lights), so
    it stays readable after dark — switching layers at sunset only made the
    regular and the "Infrarrojo" cameras look identical all night.
    """
    if force_infrared:
        return GIBS_LAYER_INFRARED, GIBS_MATRIX_SET_INFRARED
    return GIBS_LAYER_GEOCOLOR, GIBS_MATRIX_SET_GEOCOLOR


def _max_zoom_for(force_infrared: bool = False) -> int:
    return GIBS_MAX_ZOOM_INFRARED if force_infrared else GIBS_MAX_ZOOM_GEOCOLOR


_INFRARED_BLUR_RADIUS = 2.0


def _smooth_if_infrared(mosaic: Any, layer: str) -> Any:
    """Soften Band13 Clean Infrared's blocky native pixels with a light blur.

    GIBS caps this layer at zoom 6 (one level coarser than GeoColor's 7),
    and it's NASA's own pre-colored rendering, not raw data this code
    controls — verified by inspecting an unprocessed 1:1 crop straight
    from GIBS, which already shows hard-edged color blocks before this
    code touches it. There's no finer real data to request (GIBS doesn't
    publish a raw/uncolored Band13 product for GOES-East to recolor
    smoothly ourselves instead). A small Gaussian blur can't invent detail
    either, but it turns those hard block edges into something that reads
    as soft cloud texture instead of a visibly pixelated mosaic — cosmetic
    only, applied before any crop/zoom/overlay so it doesn't blur the
    outline or UI chrome drawn on top.
    """
    if layer != GIBS_LAYER_INFRARED:
        return mosaic
    from PIL import ImageFilter

    return mosaic.filter(ImageFilter.GaussianBlur(radius=_INFRARED_BLUR_RADIUS))


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
        async with _tile_semaphore():
            async with async_timeout.timeout(15):
                resp = await session.get(url)
                resp.raise_for_status()
                tile_bytes = await resp.read()
        return Image.open(io.BytesIO(tile_bytes)).convert("RGBA")
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Error fetching GIBS tile %s: %s", url, err)
        return None


_MAX_CONCURRENT_TILE_FETCHES = 12
_TILE_SEMAPHORE: asyncio.Semaphore | None = None


def _tile_semaphore() -> asyncio.Semaphore:
    """Shared cap on in-flight GIBS tile requests across every camera.

    An animation used to fire every tile of every frame at once (hundreds
    for the country camera) with a short per-request timeout — on a slower
    link the timeout expired while requests were still queued in the
    connection pool, those frames got dropped as incomplete, and the
    animation ended up with one frame (i.e. not animated). Queuing them behind a
    semaphore means the timeout only starts once a request is actually
    sent.
    """
    global _TILE_SEMAPHORE  # noqa: PLW0603
    if _TILE_SEMAPHORE is None:
        _TILE_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_TILE_FETCHES)
    return _TILE_SEMAPHORE


# Argentina doesn't observe DST, so a fixed UTC-3 offset is always correct
# (unlike using the host's local time, which may not even be Argentina's).
_ARG_UTC_OFFSET = timedelta(hours=-3)
_CAPTION_FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans.ttf")

# Reference width every overlay size below is tuned against; overlays scale
# with the frame's actual width so they keep the same proportion at any
# output size.
_REFERENCE_FRAME_WIDTH = 768


def _overlay_scale(frame: Any, override: float | None = None) -> float:
    """How much bigger/smaller `frame` is than the reference 768px frame."""
    if override is not None:
        return override
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


def _caption_bar_height(frame_width: int, has_timeline: bool) -> int:
    """Height of the bottom banner _draw_caption draws on a frame this wide."""
    scale = frame_width / _REFERENCE_FRAME_WIDTH
    font_size = round(_scaled(22, scale, 20, 38))
    padding = round(_scaled(10, scale, 10, 18))
    timeline_height = round(_scaled(22, scale, 20, 34)) if has_timeline else 0
    return font_size + 2 * padding + timeline_height


def _draw_caption(
    frame: Any,
    text: str,
    frame_index: int | None = None,
    total_frames: int | None = None,
    scale: float | None = None,
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
    mosaics — `scale` overrides that when the frame will be cropped down
    afterwards (see _overlay_scale).
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame, "RGBA")
    scale = _overlay_scale(frame, scale)

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
    coord_scale: float = 1.0,
    scale: float | None = None,
) -> None:
    """Mark the configured location with a pin, labeled with its current temperature.

    `coord_scale` maps mosaic pixels to `frame` pixels when `frame` is a
    resampled version of the mosaic `origin_x`/`origin_y` refer to (the
    region cameras draw overlays after upscaling — see _render_region_frame).

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
    x, y = (px - origin_x) * coord_scale, (py - origin_y) * coord_scale
    if not (-20 <= x <= frame.width + 20 and -20 <= y <= frame.height + 20):
        return  # Off-frame (e.g. a province view where the point falls outside it).

    draw = ImageDraw.Draw(frame, "RGBA")
    scale = _overlay_scale(frame, scale)

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


# --- Region (country / province) cameras -----------------------------------
#
# Pipeline, in this order on purpose:
#   1. Pick the highest GIBS zoom at which the area still fits a sane number
#      of source pixels (_REGION_MAX_SOURCE_PX), and fetch only the tiles that
#      actually cover the area (not a fixed square grid around it).
#   2. Crop to the area's bbox (plus a margin) and resample to a fixed output
#      size (_REGION_OUTPUT_LONG_SIDE). The satellite data itself can't gain
#      detail from this — GIBS' native resolution for a province is a few
#      hundred pixels, especially in infrared — but it's what Home
#      Assistant's dialog would otherwise do anyway, with a worse filter.
#   3. Only then draw everything vector-like (darkening, outline,
#      departments/provinces, pin, legend, caption) at that output
#      resolution, supersampled for antialiasing. Drawing them before the
#      resample — as an earlier version did — meant thin lines and text got
#      stretched along with the clouds and came out blurry/blocky.

_REGION_MAX_SOURCE_PX = 2048
_REGION_OUTPUT_LONG_SIDE = 1080
_REGION_MARGIN_RATIO = 0.05
_REGION_SUPERSAMPLE = 2
# Home Assistant's camera dialog fits the image to its width, so a very tall
# shape (Córdoba, Argentina itself) would need scrolling — pad the sides with
# the dark background instead, past this height:width ratio.
_MAX_REGION_ASPECT_RATIO = 1.5
_REGION_DARKEN_ALPHA = 220
# The region image is ~720px wide but HA's dialog shows it closer to
# 1000px, so the pin label is sized a bit above what the frame width alone
# would suggest.
_REGION_UI_SCALE = 1.5


class _RegionView:
    """Everything needed to fetch and render one area at one zoom level."""

    def __init__(self, rings: list[list[tuple[float, float]]], layer: str,
                 matrix_set: str, max_zoom: int) -> None:
        import math

        self.layer = layer
        self.matrix_set = matrix_set
        min_lat, min_lon, max_lat, max_lon = get_bbox(rings)

        zoom = max_zoom
        while True:
            x0, y0 = _deg2pixel(max_lat, min_lon, zoom)
            x1, y1 = _deg2pixel(min_lat, max_lon, zoom)
            margin = max(x1 - x0, y1 - y0) * _REGION_MARGIN_RATIO
            if max(x1 - x0, y1 - y0) + 2 * margin <= _REGION_MAX_SOURCE_PX or zoom == 0:
                break
            zoom -= 1
        self.zoom = zoom

        # Area to show, in world pixels at `zoom`. Extra room at the bottom
        # so the caption/timeline bar doesn't cover the shape's southern tip.
        self.px0, self.py0 = x0 - margin, y0 - margin
        self.px1, self.py1 = x1 + margin, y1 + margin + max(x1 - x0, y1 - y0) * 0.07

        size = SATELLITE_TILE_SIZE
        last_tile = 2 ** zoom - 1
        self.tile_x0 = math.floor(self.px0 / size)
        self.tile_x1 = math.floor((self.px1 - 1) / size)
        self.tile_y0 = max(0, math.floor(self.py0 / size))
        self.tile_y1 = min(last_tile, math.floor((self.py1 - 1) / size))
        self.mosaic_origin = (self.tile_x0 * size, self.tile_y0 * size)

        content_w, content_h = self.px1 - self.px0, self.py1 - self.py0
        self.scale = _REGION_OUTPUT_LONG_SIDE / max(content_w, content_h)
        self.content_size = (round(content_w * self.scale), round(content_h * self.scale))
        out_w, out_h = self.content_size
        if out_h > out_w * _MAX_REGION_ASPECT_RATIO:
            out_w = round(out_h / _MAX_REGION_ASPECT_RATIO)
        self.output_size = (out_w, out_h)
        self.pad_x = (out_w - self.content_size[0]) // 2

    def to_output(self, lat: float, lon: float, supersample: int = 1) -> tuple[float, float]:
        """World lat/lon -> pixel on the rendered output image."""
        px, py = _deg2pixel(lat, lon, self.zoom)
        return (
            ((px - self.px0) * self.scale + self.pad_x) * supersample,
            (py - self.py0) * self.scale * supersample,
        )


async def _fetch_region_raster(
    session: aiohttp.ClientSession, view: _RegionView, when: datetime
) -> tuple[Any, bool]:
    """Fetch the tiles covering `view` at `when`, return (cropped raster, complete)."""
    from PIL import Image

    size = SATELLITE_TILE_SIZE
    time_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    positions = [
        (tx, ty)
        for tx in range(view.tile_x0, view.tile_x1 + 1)
        for ty in range(view.tile_y0, view.tile_y1 + 1)
    ]
    tiles = await asyncio.gather(
        *(
            _fetch_tile(
                session,
                GIBS_TILE_URL_TEMPLATE.format(
                    layer=view.layer,
                    time=time_str,
                    matrix_set=view.matrix_set,
                    z=view.zoom,
                    x=tx,
                    y=ty,
                ),
            )
            for tx, ty in positions
        )
    )
    complete = all(tile is not None for tile in tiles)
    mosaic = Image.new(
        "RGBA",
        ((view.tile_x1 - view.tile_x0 + 1) * size, (view.tile_y1 - view.tile_y0 + 1) * size),
        (0, 0, 0, 255),
    )
    for (tx, ty), tile in zip(positions, tiles):
        if tile is not None:
            mosaic.paste(tile, ((tx - view.tile_x0) * size, (ty - view.tile_y0) * size))
    ox, oy = view.mosaic_origin
    crop = mosaic.crop(
        (round(view.px0 - ox), round(view.py0 - oy), round(view.px1 - ox), round(view.py1 - oy))
    )
    return crop, complete


def _render_region_frame(
    raster: Any,
    view: _RegionView,
    rings: list[list[tuple[float, float]]],
    subdivisions: list[list[tuple[float, float]]] | None,
    pin: tuple[float, float] | None,
    current_weather: dict[str, Any] | None,
    caption: str,
    frame_index: int | None = None,
    total_frames: int | None = None,
) -> Any:
    """Resample `raster` to the output size, then draw every overlay on top."""
    from PIL import Image

    raster = _smooth_if_infrared(raster, view.layer)
    if view.layer != GIBS_LAYER_INFRARED and view.scale > 1.5:
        # A large upscale of GeoColor shows pixel stair-steps; a very light
        # blur first makes LANCZOS produce smooth cloud edges instead.
        from PIL import ImageFilter

        raster = raster.filter(ImageFilter.GaussianBlur(radius=0.7))
    raster = raster.resize(view.content_size, Image.LANCZOS)
    frame = Image.new("RGBA", view.output_size, (0, 0, 0, 255))
    frame.paste(raster, (view.pad_x, 0))

    # Vector overlays drawn at 2x and downsampled: PIL's line drawing isn't
    # antialiased, so this is what keeps the outline and the dashed
    # department lines smooth instead of jagged.
    ss = _REGION_SUPERSAMPLE
    overlay = Image.new("RGBA", (frame.width * ss, frame.height * ss), (0, 0, 0, 0))
    draw_region_overlay(
        overlay,
        rings,
        lambda lat, lon: view.to_output(lat, lon, ss),
        0,
        0,
        darken_alpha=_REGION_DARKEN_ALPHA,
        subdivision_rings=subdivisions,
        scale=ss * frame.width / 400,
    )
    frame.alpha_composite(overlay.resize(frame.size, Image.LANCZOS))

    if pin:
        _draw_location_pin(
            frame,
            pin[0],
            pin[1],
            view.zoom,
            view.px0 - view.pad_x / view.scale,
            view.py0,
            current_weather,
            coord_scale=view.scale,
            scale=_REGION_UI_SCALE,
        )
    if view.layer == GIBS_LAYER_INFRARED:
        _draw_ir_legend(frame)
    _draw_caption(frame, caption, frame_index=frame_index, total_frames=total_frames)
    return frame.convert("RGB")


def _region_view_for(
    rings: list[list[tuple[float, float]]], force_infrared: bool
) -> _RegionView:
    layer, matrix_set = _layer_for(force_infrared)
    return _RegionView(rings, layer, matrix_set, _max_zoom_for(force_infrared))


# Cold cloud tops layer for the radar camera: Band13's pre-colored palette
# is gray for clear sky / warm low clouds and colored for cold (convective)
# tops, so color saturation tells them apart. Gray stays transparent;
# colored fades in between these saturations, up to this opacity.
_COLD_TOPS_MIN_SATURATION = 60
_COLD_TOPS_FULL_SATURATION = 140
_COLD_TOPS_OPACITY = 0.5
_COLD_TOPS_BLUR_RADIUS = 6  # at the radar's output size


async def fetch_cold_cloud_tops_rasters(
    session: aiohttp.ClientSession,
    origin_x: float,
    origin_y: float,
    size: int,
    zoom: int,
    times: list[datetime],
) -> list[Any | None]:
    """Band13 infrared for a square area, one raster per `times` (oldest first).

    The area is `size` world pixels at `zoom` starting at (origin_x,
    origin_y) — the radar camera's own mosaic — fetched at GIBS' max
    infrared zoom; cold_cloud_tops_layer scales it to match, like radar.py
    does with RainViewer's tiles. GIBS publishes 10-40 min behind (and a
    frame's tiles incrementally), so a time without a complete frame gets
    the closest earlier one that has it, or None.

    Its tiles go through radar.py's tile queue, not the shared GIBS one
    (_tile_semaphore): a satellite animation's hundreds of queued tiles
    would hold up the radar camera, which must stay quick (see camera.py).
    """
    from PIL import Image

    tile = SATELLITE_TILE_SIZE
    scale = 2.0 ** (GIBS_MAX_ZOOM_INFRARED - zoom)
    x0, y0, size_ir = origin_x * scale, origin_y * scale, size * scale
    tx0, tx1 = int(x0 // tile), int((x0 + size_ir) // tile)
    ty0, ty1 = int(y0 // tile), int((y0 + size_ir) // tile)
    positions = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    left, top = round(x0 - tx0 * tile), round(y0 - ty0 * tile)

    async def fetch(when: datetime) -> Any | None:
        time_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        tiles = await asyncio.gather(
            *(
                _fetch_tile_unqueued(
                    session,
                    GIBS_TILE_URL_TEMPLATE.format(
                        layer=GIBS_LAYER_INFRARED,
                        time=time_str,
                        matrix_set=GIBS_MATRIX_SET_INFRARED,
                        z=GIBS_MAX_ZOOM_INFRARED,
                        x=tx,
                        y=ty,
                    ),
                )
                for tx, ty in positions
            )
        )
        # Band13 tiles are fully opaque; a failed fetch comes back fully transparent.
        if not all(tile_img.getchannel("A").getbbox() for tile_img in tiles):
            return None
        raw = Image.new("RGBA", ((tx1 - tx0 + 1) * tile, (ty1 - ty0 + 1) * tile))
        for (tx, ty), tile_img in zip(positions, tiles):
            raw.paste(tile_img, ((tx - tx0) * tile, (ty - ty0) * tile))
        return raw.crop((left, top, left + round(size_ir), top + round(size_ir)))

    rasters = await asyncio.gather(*(fetch(when) for when in times))
    filled: list[Any | None] = []
    latest = None
    for raster in rasters:
        latest = raster or latest
        filled.append(latest)
    return filled


def cold_cloud_tops_layer(raster: Any, size: int) -> Any | None:
    """Translucent `size`-px layer with only `raster`'s cold cloud tops, or None if none.

    Blurred and masked at the raster's own (coarse) resolution, then scaled
    up: smooth, and much cheaper than doing it at full size.
    """
    from PIL import Image, ImageFilter

    blurred = raster.convert("RGB").filter(
        ImageFilter.GaussianBlur(radius=_COLD_TOPS_BLUR_RADIUS * raster.width / size)
    )
    span = _COLD_TOPS_FULL_SATURATION - _COLD_TOPS_MIN_SATURATION
    mask = (
        blurred.convert("HSV")
        .getchannel("S")
        .point(
            lambda s: 0
            if s < _COLD_TOPS_MIN_SATURATION
            else round(min(1, (s - _COLD_TOPS_MIN_SATURATION) / span) * 255 * _COLD_TOPS_OPACITY)
        )
    )
    if mask.getbbox() is None:
        return None
    layer = blurred.resize((size, size), Image.BICUBIC).convert("RGBA")
    layer.putalpha(mask.resize((size, size), Image.BICUBIC))
    return layer


async def build_region_animation_frames(
    session: aiohttp.ClientSession,
    rings: list[list[tuple[float, float]]],
    force_infrared: bool = False,
    pin: tuple[float, float] | None = None,
    current_weather: dict[str, Any] | None = None,
    subdivisions: list[list[tuple[float, float]]] | None = None,
) -> list[Any] | None:
    """Rendered frames (oldest first) of the last SATELLITE_ANIMATION_FRAMES of a whole area.

    See the comment at the top of this section for the rendering; encode with
    encode_mp4(). More candidate timestamps than needed are fetched
    (SATELLITE_ANIMATION_LOOKBACK_BUFFER) so a few incomplete ones don't
    shrink the animation; the most recent complete ones are kept.
    """
    if not rings:
        return None
    view = _region_view_for(rings, force_infrared)
    latest_time = await _resolve_latest_frame_time(session, view.layer, view.matrix_set)
    if latest_time is None:
        _LOGGER.warning("No recent GIBS frame found for layer %s", view.layer)
        return None
    times = _recent_frame_times(
        latest_time, SATELLITE_ANIMATION_FRAMES + SATELLITE_ANIMATION_LOOKBACK_BUFFER
    )
    results = await asyncio.gather(
        *(_fetch_region_raster(session, view, when) for when in times)
    )
    kept = [(raster, when) for (raster, complete), when in zip(results, times) if complete]
    kept = kept[-SATELLITE_ANIMATION_FRAMES:]
    if not kept:
        return None

    def render() -> list[Any]:
        total = len(kept)
        return [
            _render_region_frame(
                raster,
                view,
                rings,
                subdivisions,
                pin,
                current_weather,
                _format_frame_caption(when),
                frame_index=i,
                total_frames=total,
            )
            for i, (raster, when) in enumerate(kept)
        ]

    return await asyncio.get_running_loop().run_in_executor(None, render)


# --- Encoding ---------------------------------------------------------------

# Crossfaded in-between frames so the loop looks smooth instead of jumping
# every ~half second. Cheap (a Pillow blend per in-between frame), and
# H.264 compresses them well.
_VIDEO_FPS = 12
_VIDEO_INBETWEENS = 5
_VIDEO_HOLD_LAST_SECONDS = 1.5


def encode_gif(frames: list[Any], frame_ms: int = 450, hold_last_ms: int = 1500) -> bytes:
    """Looping GIF of `frames`: the camera image the dashboard shows.

    A dashboard renders the camera image in an <img>, which animates a GIF
    in every browser but plays an MP4 only in Safari (the MP4 goes out
    separately, see encode_mp4). Only the real frames, without encode_mp4's
    in-betweens: every GIF frame costs almost a full frame of file size.
    The whole loop stays well under the ~10 s a camera card waits before
    reloading the image, so it's always seen through to the latest frame,
    which is held longer.

    No dithering: Pillow's default RGB->palette conversion dithers, which
    turns thin antialiased lines and text into speckled noise once
    quantized to 256 colors.
    """
    from PIL import Image

    try:
        no_dither = Image.Dither.NONE
    except AttributeError:  # Pillow < 9.1
        no_dither = Image.NONE
    paletted = [f.quantize(colors=256, dither=no_dither) for f in frames]
    durations = [frame_ms] * (len(paletted) - 1) + [hold_last_ms]
    buffer = io.BytesIO()
    paletted[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=paletted[1:],
        duration=durations,
        loop=0,
        disposal=1,
    )
    return buffer.getvalue()


def encode_mp4(frames: list[Any]) -> bytes | None:
    """H.264 MP4 of `frames` with crossfaded in-betweens, or None without PyAV.

    PyAV ships with Home Assistant itself (its `stream` integration uses
    it), so this adds no dependency in practice; it's imported lazily so a
    setup without it just logs a warning instead of failing to load.
    """
    try:
        import av
    except ImportError:
        _LOGGER.warning("PyAV not available, satellite video cameras won't have an image")
        return None
    from PIL import Image

    # yuv420p needs even dimensions.
    width, height = frames[0].size
    width, height = width - width % 2, height - height % 2
    frames = [f if f.size == (width, height) else f.crop((0, 0, width, height)) for f in frames]

    # The caption/timeline bar isn't blended (it would overlap two
    # timestamps): in-betweens take it from whichever real frame is nearer.
    bar = min(height, _caption_bar_height(width, len(frames) > 1) + 2)
    bar_box = (0, height - bar, width, height)
    sequence: list[Any] = []
    for current, following in zip(frames, frames[1:]):
        sequence.append(current)
        for step in range(1, _VIDEO_INBETWEENS + 1):
            weight = step / (_VIDEO_INBETWEENS + 1)
            blended = Image.blend(current, following, weight)
            nearest = current if weight < 0.5 else following
            blended.paste(nearest.crop(bar_box), (0, height - bar))
            sequence.append(blended)
    sequence.extend([frames[-1]] * max(1, round(_VIDEO_FPS * _VIDEO_HOLD_LAST_SECONDS)))

    # `faststart` (index at the start of the file, which messaging apps
    # expect) rewrites the file at close, so it needs a real path rather
    # than an in-memory buffer.
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)
    try:
        container = av.open(path, mode="w", options={"movflags": "faststart"})
        stream = container.add_stream("libx264", rate=_VIDEO_FPS)
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "23", "preset": "veryfast"}
        for image in sequence:
            for packet in stream.encode(av.VideoFrame.from_image(image)):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
        container.close()
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)
