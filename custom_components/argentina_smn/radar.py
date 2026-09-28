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
from typing import Any

import aiohttp
import async_timeout

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
    session: aiohttp.ClientSession, latitude: float, longitude: float
) -> bytes | None:
    """Build an animated GIF of the last few radar frames around lat/lon."""
    try:
        host, frame_paths = await _fetch_frame_paths(session)
    except (aiohttp.ClientError, KeyError) as err:
        _LOGGER.warning("Error fetching RainViewer frame index: %s", err)
        return None

    if not frame_paths:
        return None

    center_x, center_y = _deg2tile(latitude, longitude, RADAR_ZOOM)

    basemap = await _fetch_basemap_mosaic(session, center_x, center_y)

    frames = []
    for frame_path in frame_paths:
        frame = await _fetch_radar_mosaic(
            session, host, frame_path, center_x, center_y, basemap
        )
        frames.append(frame)

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
