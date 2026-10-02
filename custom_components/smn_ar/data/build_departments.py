"""One-off script that built ar_departamentos.geojson — not run at runtime.

Regenerate like this if it's ever needed again (e.g. geoBoundaries ships a
revision): requires `pip install shapely`, and a local copy of
geoBoundaries' ARG ADM2 simplified GeoJSON (get the current download URL
from https://www.geoboundaries.org/api/current/gbOpen/ARG/ADM2/, field
`simplifiedGeometryGeoJSON`) saved as /tmp/adm2.geojson.

ADM2's own properties don't say which province each department belongs to
(only its name) — this resolves that via a spatial join against the
already-bundled ar_provincias.geojson (ADM1), not a lookup table, so it
stays correct even if geoBoundaries renames/restructures provinces later.
See the matching logic below for why it's centroid-containment first,
max-overlap-area as a fallback (and why each handles different edge cases
better than a single method would).
"""
import json
import os

from shapely.geometry import MultiPolygon, Polygon, shape


def _polygons_only(geom):
    """Keep only the polygonal parts of a geometry.

    An intersection between two polygons can occasionally return a
    GeometryCollection mixing slivers of LineString/Point with the actual
    polygon(s) at shared-edge touches — only the polygons matter for
    drawing a filled region's outline.
    """
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        if not polys:
            return geom
        return polys[0] if len(polys) == 1 else MultiPolygon(
            [p for poly in polys for p in (poly.geoms if isinstance(poly, MultiPolygon) else [poly])]
        )
    return geom

_DATA_DIR = os.path.dirname(__file__)

provinces = json.load(open(os.path.join(_DATA_DIR, "ar_provincias.geojson"), encoding="utf-8"))
province_shapes = [
    (f["properties"]["name"], shape(f["geometry"]))
    for f in provinces["features"]
    if f["properties"]["name"] != "Argentina"
]
province_by_name = dict(province_shapes)

adm2 = json.load(open("/tmp/adm2.geojson", encoding="utf-8"))


def round_coords(coords, ndigits=4):
    if isinstance(coords[0], (list, tuple)):
        return [round_coords(c, ndigits) for c in coords]
    return [round(coords[0], ndigits), round(coords[1], ndigits)]


out_features = []
unmatched = []
for feat in adm2["features"]:
    name = feat["properties"].get("shapeName")
    if not name:
        continue
    geom = shape(feat["geometry"])
    dept_area = geom.area or 1.0
    centroid = geom.representative_point()

    def overlap_ratio(pshape, _geom=geom, _area=dept_area):
        try:
            return pshape.intersection(_geom).area / _area
        except Exception:  # noqa: BLE001
            return 0.0

    # Centroid-containment is the primary test (fast, and correct for the
    # vast majority — including coastal/river-bordering partidos whose
    # polygon includes water area the simplified province outline doesn't,
    # which would otherwise look like a low-overlap province). It's only
    # overridden when that candidate's overlap ratio is implausibly low,
    # which is what actually happens for the ~5 department names reused
    # across different provinces nationwide (e.g. "Rivadavia") — the
    # simplified ADM1 polygons occasionally put a homonym's centroid just
    # inside the wrong neighboring province.
    candidate = None
    for pname, pshape in province_shapes:
        if pshape.contains(centroid):
            candidate = pname
            break

    province_name = None
    if candidate is not None and overlap_ratio(province_by_name[candidate]) >= 0.3:
        province_name = candidate
    else:
        # No confident centroid-based candidate — fall back to whichever
        # province overlaps this department's area the most at all. Some
        # legitimate coastal/river-bordering departments (e.g. Avellaneda,
        # Buenos Aires) have a low ratio here simply because their polygon
        # includes water area the simplified province outline doesn't — so
        # this takes whatever's best rather than requiring a minimum, and
        # only gives up when there's truly zero overlap with anything
        # (e.g. CABA's comunas against our bundled CABA polygon).
        best_overlap = 0.0
        for pname, pshape in province_shapes:
            ratio = overlap_ratio(pshape)
            if ratio > best_overlap:
                best_overlap = ratio
                province_name = pname
        if province_name is None or best_overlap <= 0:
            unmatched.append(name)
            continue

    # geoBoundaries simplifies ADM1 (provinces) and ADM2 (departments)
    # independently, so their lines don't land on the exact same pixels —
    # a department's real border can poke slightly outside our own
    # province polygon's simplified edge. Clip every department to its own
    # province's polygon so department lines never cross the province
    # outline we actually draw — more important for visual consistency on
    # a small satellite image than matching the department's literal
    # official shape to the last meter.
    clipped = _polygons_only(geom.intersection(province_by_name[province_name]))
    if not clipped.is_empty:
        geom = clipped

    # Department outlines are only ever drawn at a coarse zoom (same tile
    # grid as the province/country overlays, max GIBS zoom 6-7) — simplify
    # before bundling so the file (and the per-request draw cost) doesn't
    # carry detail no camera image can actually show. A finer tolerance
    # than before (0.003 vs 0.01) — the clipping above already removed the
    # worst mismatches, this is just for file size/draw cost.
    simplified = geom.simplify(0.003, preserve_topology=True)
    if simplified.is_empty:
        simplified = geom
    simple_geojson = json.loads(json.dumps(simplified.__geo_interface__))
    out_features.append({
        "type": "Feature",
        "properties": {"name": name, "province": province_name},
        "geometry": {
            "type": simple_geojson["type"],
            "coordinates": round_coords(simple_geojson["coordinates"]),
        },
    })

print(f"total: {len(adm2['features'])}, matched: {len(out_features)}, unmatched: {unmatched}")

with open(os.path.join(_DATA_DIR, "ar_departamentos.geojson"), "w", encoding="utf-8") as fh:
    json.dump({"type": "FeatureCollection", "features": out_features}, fh, ensure_ascii=False, separators=(",", ":"))
