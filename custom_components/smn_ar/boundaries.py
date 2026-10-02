"""Province/department outline overlays, drawn on radar/satellite mosaics.

Boundaries come from geoBoundaries.org's open ADM1 (provinces) and ADM2
(departments/partidos) datasets for Argentina (CC BY 4.0,
https://www.geoboundaries.org, sourced from IGN/Wikimedia), bundled locally
as data/ar_provincias.geojson and data/ar_departamentos.geojson —
simplified and trimmed to just name/province + geometry to keep them
small. Not fetched live: these borders don't change, so there's no reason
to depend on an external service (or its availability) just to draw a
fixed outline.

ADM2's department name isn't tied to its parent province in the raw data
(only ADM1's own polygons are), so ar_departamentos.geojson's `province`
field was resolved offline via a one-off spatial join (each department
matched to whichever province polygon covers most of its area) before
bundling — see the data/ directory for how it was built if it ever needs
regenerating. A handful of edge cases (CABA's comunas, a couple of
departments split awkwardly by the ADM1/ADM2 simplification not lining up
exactly at the border) didn't resolve cleanly and are simply left out —
cosmetic gaps in the subdivision overlay, not in the province/country
outline itself.
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
_DEPARTMENTS_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "ar_departamentos.geojson")


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


def get_all_province_rings() -> list[list[tuple[float, float]]]:
    """Return every province's rings flattened together (not the country outline).

    Used to draw the internal province borders on the whole-country map —
    each ring is still a closed loop drawn independently (see
    draw_province_outline), so flattening every province into one list
    doesn't connect unrelated provinces' lines to each other.
    """
    return [
        ring
        for name, rings in _load_provinces().items()
        if name != "argentina"
        for ring in rings
    ]


@functools.lru_cache(maxsize=1)
def _load_departments() -> dict[str, list[list[tuple[float, float]]]]:
    """Load the bundled department (ADM2) outlines, keyed by normalized province name.

    Each province maps to every one of its departments' rings flattened
    together (same reasoning as get_all_province_rings — rings are drawn
    independently, so there's no need to keep departments distinguished
    from each other, only from other provinces').
    """
    try:
        with open(_DEPARTMENTS_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.warning("Could not load bundled department boundaries: %s", err)
        return {}

    by_province: dict[str, list[list[tuple[float, float]]]] = {}
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        province = props.get("province")
        geometry = feature.get("geometry") or {}
        if not province or not geometry:
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
        by_province.setdefault(_normalize(province), []).extend(rings)
    return by_province


def get_department_rings(province_name: str) -> list[list[tuple[float, float]]] | None:
    """Return every department's rings for `province_name`, or None if not found."""
    if not province_name:
        return None
    return _load_departments().get(_normalize(province_name))


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
    color: tuple[int, int, int, int] = (255, 191, 105, 225),
    width: int | None = None,
    scale: float | None = None,
) -> None:
    """Draw a province's outline on `frame`, given a lon/lat -> world-pixel function.

    `deg2pixel(lat, lon) -> (px, py)` and `(origin_x, origin_y)` are the
    same projection radar.py/satellite.py already use for their mosaics
    (Web Mercator world pixels minus the mosaic's own top-left corner), so
    this overlay lines up with whatever imagery is already on `frame`.

    Default is a soft amber — distinct from both the white/gray clouds and
    the thin gray dashed lines draw_subdivision_lines uses for
    departments/provinces, so the two never read as the same kind of line.
    Plain white/gray disappeared against cloud cover, and a saturated
    pink/red worked but read as too loud/"primary" next to the
    darkened-outside overlay (draw_region_overlay), which already does
    most of the work of making the area stand out — the halo is what keeps
    a *soft* color visible on bright cloud without needing a louder one.
    """
    from PIL import ImageDraw

    if width is None:
        # Scaled against the same 768px reference frame satellite.py's
        # overlays use, so the outline doesn't look thin on the much
        # bigger country/province mosaics (1536px+) or heavy on the small
        # local camera. `scale` lets a caller override what "the frame's
        # own size" means — e.g. when it's about to be cropped down to a
        # much smaller final image, so the stroke should be sized for
        # *that*, not for the pre-crop canvas it's actually drawn on.
        effective_scale = scale if scale is not None else frame.width / 768
        width = max(2, min(5, round(2 * effective_scale)))

    draw = ImageDraw.Draw(frame, "RGBA")
    for ring in rings:
        points = []
        for lon, lat in ring:
            px, py = deg2pixel(lat, lon)
            points.append((px - origin_x, py - origin_y))
        if len(points) >= 2:
            closed = points + [points[0]]
            draw.line(closed, fill=(0, 0, 0, 130), width=width + 2)
            draw.line(closed, fill=color, width=width)


def _dashed_polyline(
    draw: Any,
    points: list[tuple[float, float]],
    color: tuple[int, int, int, int],
    width: int,
    dash: float,
    gap: float,
) -> None:
    """Draw `points` as a dashed line (PIL has no native dash support)."""
    import math

    remaining = dash
    drawing = True
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        seg_len = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while pos < seg_len:
            step = min(remaining, seg_len - pos)
            if drawing and step > 0:
                t0, t1 = pos / seg_len, (pos + step) / seg_len
                draw.line(
                    [(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                     (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)],
                    fill=color, width=width,
                )
            pos += step
            remaining -= step
            if remaining <= 0:
                drawing = not drawing
                remaining = dash if drawing else gap


def draw_subdivision_lines(
    frame: Any,
    rings: list[list[tuple[float, float]]],
    deg2pixel: Any,
    origin_x: float,
    origin_y: float,
    color: tuple[int, int, int, int] = (235, 238, 240, 115),
    scale: float | None = None,
) -> None:
    """Draw internal borders (departments within a province, provinces within
    the country) as thin, dashed, translucent lines — deliberately a
    different *kind* of line (dashed, gray, near-transparent) from
    draw_province_outline's solid amber, so with dozens of departments on
    screen at once they read as background reference, not as more of
    "the" boundary. No dark halo: a halo on every dash would turn into
    visual clutter at this density. See draw_province_outline for what
    `scale` overrides.
    """
    from PIL import ImageDraw

    effective_scale = scale if scale is not None else frame.width / 768
    width = max(1, round(effective_scale))
    dash = max(3, round(5 * effective_scale))
    gap = max(3, round(4 * effective_scale))
    draw = ImageDraw.Draw(frame, "RGBA")
    for ring in rings:
        points = []
        for lon, lat in ring:
            px, py = deg2pixel(lat, lon)
            points.append((px - origin_x, py - origin_y))
        if len(points) >= 2:
            _dashed_polyline(draw, points + [points[0]], color, width, dash, gap)


def draw_region_overlay(
    frame: Any,
    rings: list[list[tuple[float, float]]],
    deg2pixel: Any,
    origin_x: float,
    origin_y: float,
    darken_alpha: int = 130,
    subdivision_rings: list[list[tuple[float, float]]] | None = None,
    scale: float | None = None,
) -> None:
    """Darken everything outside `rings` (a spotlight effect), then draw the outline.

    Makes the country/province stand out even where the outline color
    alone would be hard to pick out (e.g. thin slivers of a wiggly
    border), and reads immediately as "this is the area that matters"
    without having to trace the line. Rings with holes aren't a concern
    here (Argentina's provinces are all simple polygons), so this just
    unions every ring's filled interior into one "keep lit" mask. `scale`
    is passed through to draw_province_outline/draw_subdivision_lines —
    see there for what it overrides.
    """
    from PIL import Image, ImageDraw

    mask = Image.new("L", frame.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    for ring in rings:
        points = [
            (px - origin_x, py - origin_y)
            for lon, lat in ring
            for px, py in [deg2pixel(lat, lon)]
        ]
        if len(points) >= 3:
            mask_draw.polygon(points, fill=255)

    alpha = mask.point(lambda inside: 0 if inside else darken_alpha)
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 255))
    overlay.putalpha(alpha)
    frame.alpha_composite(overlay)

    if subdivision_rings:
        draw_subdivision_lines(frame, subdivision_rings, deg2pixel, origin_x, origin_y, scale=scale)
    draw_province_outline(frame, rings, deg2pixel, origin_x, origin_y, scale=scale)
