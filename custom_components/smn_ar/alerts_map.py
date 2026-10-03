"""Whole-country map of SMN's active short-term warnings (avisos a muy corto plazo).

Same idea as the radar camera's alert polygons, zoomed out to all of
Argentina: an OpenStreetMap basemap, province borders, and every active
warning/shortterm polygon nationwide (SMN's own geometry), with the
configured location pinned. Reuses the region framing/overlay pipeline from
satellite.py (see the comment at the top of its region section).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import io
import logging
from typing import Any

import aiohttp

from .boundaries import draw_region_overlay, get_all_province_rings, get_country_rings
from .const import BASEMAP_TILE_URL_TEMPLATE, BASEMAP_USER_AGENT, SATELLITE_TILE_SIZE
from .radar import _draw_storm_icon, _fetch_tile
from .satellite import (
    _REGION_SUPERSAMPLE,
    _RegionView,
    _draw_caption,
    _draw_location_pin,
    _REGION_UI_SCALE,
)

_LOGGER = logging.getLogger(__name__)

# OSM's labels are only legible up to a modest downscale, and zoom 5 already
# frames the whole country in ~1000 source pixels.
_BASEMAP_MAX_ZOOM = 5
# Lighter than the satellite views' darkening: neighboring countries stay
# readable as geographic context on a basemap.
_OUTSIDE_DARKEN_ALPHA = 110
# Dark dashes: the satellite views' light-gray ones vanish on OSM's light map.
_PROVINCE_LINE = (70, 70, 85, 190)
_ALERT_FILL = (230, 30, 30, 70)
_ALERT_OUTLINE = (210, 20, 20, 255)
_ARG_TZ = timezone(timedelta(hours=-3))


async def _fetch_basemap(session: aiohttp.ClientSession, view: _RegionView) -> Any:
    from PIL import Image

    size = SATELLITE_TILE_SIZE
    positions = [
        (tx, ty)
        for tx in range(view.tile_x0, view.tile_x1 + 1)
        for ty in range(view.tile_y0, view.tile_y1 + 1)
    ]
    tiles = await asyncio.gather(
        *(
            _fetch_tile(
                session,
                BASEMAP_TILE_URL_TEMPLATE.format(z=view.zoom, x=tx, y=ty),
                headers={"User-Agent": BASEMAP_USER_AGENT},
            )
            for tx, ty in positions
        )
    )
    mosaic = Image.new(
        "RGBA",
        ((view.tile_x1 - view.tile_x0 + 1) * size, (view.tile_y1 - view.tile_y0 + 1) * size),
        (40, 40, 45, 255),
    )
    for (tx, ty), tile in zip(positions, tiles):
        mosaic.alpha_composite(tile, ((tx - view.tile_x0) * size, (ty - view.tile_y0) * size))
    ox, oy = view.mosaic_origin
    return mosaic.crop(
        (round(view.px0 - ox), round(view.py0 - oy), round(view.px1 - ox), round(view.py1 - oy))
    )


def _render(
    basemap: Any,
    view: _RegionView,
    alerts: list[dict[str, Any]],
    pin: tuple[float, float],
) -> bytes:
    from PIL import Image, ImageDraw

    frame = Image.new("RGBA", view.output_size, (40, 40, 45, 255))
    frame.paste(basemap.resize(view.content_size, Image.LANCZOS), (view.pad_x, 0))

    ss = _REGION_SUPERSAMPLE
    overlay = Image.new("RGBA", (frame.width * ss, frame.height * ss), (0, 0, 0, 0))
    to_px = lambda lat, lon: view.to_output(lat, lon, ss)  # noqa: E731
    draw_region_overlay(
        overlay,
        get_country_rings(),
        to_px,
        0,
        0,
        darken_alpha=_OUTSIDE_DARKEN_ALPHA,
        subdivision_rings=get_all_province_rings(),
        scale=ss * frame.width / 400,
        subdivision_color=_PROVINCE_LINE,
    )

    draw = ImageDraw.Draw(overlay, "RGBA")
    icons = []
    for alert in alerts:
        for ring in (alert.get("geometry") or {}).get("coordinates") or []:
            points = [to_px(lat, lon) for lon, lat in ring]
            if len(points) < 3:
                continue
            draw.polygon(points, fill=_ALERT_FILL, outline=_ALERT_OUTLINE, width=3 * ss)
            icons.append(
                (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))
            )
    for cx, cy in icons:
        _draw_storm_icon(draw, cx, cy, size=18 * ss)
    frame.alpha_composite(overlay.resize(frame.size, Image.LANCZOS))

    _draw_location_pin(
        frame,
        pin[0],
        pin[1],
        view.zoom,
        view.px0 - view.pad_x / view.scale,
        view.py0,
        None,  # no temperature label: it would cover nearby warning polygons
        coord_scale=view.scale,
        scale=_REGION_UI_SCALE,
    )

    now = datetime.now(_ARG_TZ).strftime("%H:%M")
    if not alerts:
        caption = f"Sin avisos vigentes · {now}"
    elif len(alerts) == 1:
        caption = f"1 aviso vigente · {now}"
    else:
        caption = f"{len(alerts)} avisos vigentes · {now}"
    _draw_caption(frame, caption)

    buffer = io.BytesIO()
    frame.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def build_country_alerts_jpeg(
    session: aiohttp.ClientSession,
    alerts: list[dict[str, Any]],
    pin: tuple[float, float],
) -> bytes | None:
    """JPEG of Argentina with every active nationwide short-term warning polygon."""
    rings = get_country_rings()
    if not rings:
        return None
    view = _RegionView(rings, layer="", matrix_set="", max_zoom=_BASEMAP_MAX_ZOOM)
    basemap = await _fetch_basemap(session, view)
    return await asyncio.get_running_loop().run_in_executor(
        None, _render, basemap, view, alerts, pin
    )
