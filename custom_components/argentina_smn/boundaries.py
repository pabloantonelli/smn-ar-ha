"""Province outline overlay, drawn on radar/satellite mosaics.

Boundaries come from geoBoundaries.org's open ADM1 dataset for Argentina
(CC BY 4.0, https://www.geoboundaries.org, sourced from IGN/Wikimedia),
bundled locally as data/ar_provincias.geojson — simplified and trimmed to
just name + geometry to keep it small (~80KB). Not fetched live: province
borders don't change, so there's no reason to depend on an external
service (or its availability) just to draw a fixed outline.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import unicodedata
from typing import Any

_LOGGER = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "ar_provincias.geojson")


def _normalize(name: str) -> str:
    """Lowercase and strip accents so SMN's and geoBoundaries' spellings match."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip().lower()


@functools.lru_cache(maxsize=1)
def _load_provinces() -> dict[str, list[list[tuple[float, float]]]]:
    """Load the bundled GeoJSON once, keyed by normalized province name.

    Each value is a list of rings, each ring a list of (lon, lat) tuples —
    flattened from Polygon/MultiPolygon geometries, since only the outline
    is drawn (no need to keep them distinguished).
    """
    try:
        with open(_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not load bundled province boundaries: %s", err)
        return {}

    provinces: dict[str, list[list[tuple[float, float]]]] = {}
    for feature in data.get("features", []):
        name = (feature.get("properties") or {}).get("name")
        geometry = feature.get("geometry") or {}
        if not name or not geometry:
            continue
        geom_type = geometry.get("type")
        coordinates = geometry.get("coordinates") or []
        if geom_type == "Polygon":
            polygons = [coordinates]
        elif geom_type == "MultiPolygon":
            polygons = coordinates
        else:
            continue
        rings = [
            [(lon, lat) for lon, lat in ring]
            for polygon in polygons
            for ring in polygon
        ]
        provinces[_normalize(name)] = rings
    return provinces


def get_province_rings(province_name: str) -> list[list[tuple[float, float]]] | None:
    """Return the outline rings for `province_name`, or None if not found."""
    if not province_name:
        return None
    return _load_provinces().get(_normalize(province_name))


def get_country_rings() -> list[list[tuple[float, float]]]:
    """Return Argentina's national outline."""
    return _load_provinces().get("argentina", [])


def get_bbox(rings: list[list[tuple[float, float]]]) -> tuple[float, float, float, float]:
    """Return (min_lat, min_lon, max_lat, max_lon) covering all of `rings`."""
    lats = [lat for ring in rings for _, lat in ring]
    lons = [lon for ring in rings for lon, _ in ring]
    return min(lats), min(lons), max(lats), max(lons)


def draw_province_outline(
    frame: Any,
    rings: list[list[tuple[float, float]]],
    deg2pixel: Any,
    origin_x: float,
    origin_y: float,
    color: tuple[int, int, int, int] = (235, 235, 235, 210),
    width: int = 1,
) -> None:
    """Draw a province's outline on `frame`, given a lon/lat -> world-pixel function.

    `deg2pixel(lat, lon) -> (px, py)` and `(origin_x, origin_y)` are the
    same projection radar.py/satellite.py already use for their mosaics
    (Web Mercator world pixels minus the mosaic's own top-left corner), so
    this overlay lines up with whatever imagery is already on `frame`.

    Default color is a light, thin line — meant to read as a subtle map
    reference, not compete visually with cloud/alert overlays.
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(frame, "RGBA")
    for ring in rings:
        points = []
        for lon, lat in ring:
            px, py = deg2pixel(lat, lon)
            points.append((px - origin_x, py - origin_y))
        if len(points) >= 2:
            draw.line(points + [points[0]], fill=color, width=width)
