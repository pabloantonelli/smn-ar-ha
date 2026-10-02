"""One-off fix applied to ar_provincias.geojson — not run at runtime.

geoBoundaries' ARG ADM1 release has no Entre Ríos feature: its territory is
merged into the "Buenos Aires" polygon. The ADM2 (departments) release does
have Entre Ríos' 17 departments, so this rebuilds the province as their
union and subtracts it from Buenos Aires. Run before build_departments.py
(which assigns departments to provinces spatially).

Requires `pip install shapely` and geoBoundaries' ADM2 simplified GeoJSON
at /tmp/adm2.geojson (see build_departments.py).
"""
import json
import os

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

_DATA_DIR = os.path.dirname(__file__)
_PATH = os.path.join(_DATA_DIR, "ar_provincias.geojson")

# Entre Ríos' departments; a bounding box disambiguates names that repeat
# in other provinces (Colón, La Paz, San Salvador, Victoria, Federal...).
ENTRE_RIOS_DEPARTMENTS = {
    "Colón", "Concordia", "Diamante", "Federación", "Federal", "Feliciano",
    "Gualeguay", "Gualeguaychú", "Islas del Ibicuy", "La Paz", "Nogoya",
    "Paraná", "San Salvador", "Tala", "Uruguay", "Victoria", "Villaguay",
}
ENTRE_RIOS_BBOX = (-61.0, -34.1, -57.7, -30.1)  # min_lon, min_lat, max_lon, max_lat


def _round(coords, ndigits=4):
    if isinstance(coords[0], (list, tuple)):
        return [_round(c, ndigits) for c in coords]
    return [round(coords[0], ndigits), round(coords[1], ndigits)]


def main() -> None:
    adm2 = json.load(open("/tmp/adm2.geojson", encoding="utf-8"))
    parts = []
    for feat in adm2["features"]:
        if feat["properties"].get("shapeName") not in ENTRE_RIOS_DEPARTMENTS:
            continue
        geom = shape(feat["geometry"])
        point = geom.representative_point()
        min_lon, min_lat, max_lon, max_lat = ENTRE_RIOS_BBOX
        if min_lon < point.x < max_lon and min_lat < point.y < max_lat:
            parts.append(geom.buffer(0))
    if len(parts) != len(ENTRE_RIOS_DEPARTMENTS):
        raise SystemExit(f"expected {len(ENTRE_RIOS_DEPARTMENTS)} departments, found {len(parts)}")
    # A tiny buffer out-and-back closes slivers between adjacent departments.
    entre_rios = unary_union(parts).buffer(0.002).buffer(-0.002).simplify(0.003)

    data = json.load(open(_PATH, encoding="utf-8"))
    features = [f for f in data["features"] if f["properties"]["name"] != "Entre Ríos"]
    for feat in features:
        if feat["properties"]["name"] == "Buenos Aires":
            buenos_aires = shape(feat["geometry"]).buffer(0).difference(entre_rios.buffer(0.01))
            feat["geometry"] = mapping(buenos_aires)
            feat["geometry"]["coordinates"] = _round(json.loads(json.dumps(feat["geometry"]["coordinates"])))
    features.append({
        "type": "Feature",
        "properties": {"name": "Entre Ríos"},
        "geometry": {
            **mapping(entre_rios),
            "coordinates": _round(json.loads(json.dumps(mapping(entre_rios)["coordinates"]))),
        },
    })
    data["features"] = features
    with open(_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print("Entre Ríos added; Buenos Aires corrected")


if __name__ == "__main__":
    main()
