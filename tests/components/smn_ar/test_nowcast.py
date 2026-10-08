"""Test the radar/infrared nowcast (nowcast.py)."""
from datetime import datetime, timedelta, timezone

import numpy as np
from PIL import Image
import pytest

from custom_components.smn_ar.nowcast import (
    MOTION_WIND,
    SOURCE_RADAR,
    SOURCE_SATELLITE,
    Field,
    Motion,
    agrees_with,
    aviso_arrival,
    decode_infrared,
    decode_radar,
    estimate_motion,
    project_arrival,
    run_nowcast,
    steering_motion,
)

T0 = datetime(2026, 10, 8, 18, 0, tzinfo=timezone.utc)
SIZE = 120
KM_PER_PX = 2.0
CENTER = (SIZE / 2, SIZE / 2)


def _blob(cx: float, cy: float, value: float = 55, radius: float = 6) -> np.ndarray:
    ys, xs = np.mgrid[0:SIZE, 0:SIZE]
    frame = np.zeros((SIZE, SIZE), dtype=np.float32)
    frame[(xs - cx) ** 2 + (ys - cy) ** 2 <= radius**2] = value
    # Some texture, like real echoes, plus a second, smaller cell.
    frame[(xs - cx - 3) ** 2 + (ys - cy + 2) ** 2 <= 4] = value + 5
    frame[(xs - cx + 25) ** 2 + (ys - cy - 15) ** 2 <= 9] = value - 5
    return frame


def _field(positions: list[tuple[float, float]], minutes_apart: int = 10, source: str = SOURCE_SATELLITE) -> Field:
    count = len(positions)
    return Field(
        source=source,
        times=[T0 - timedelta(minutes=minutes_apart * (count - 1 - i)) for i in range(count)],
        frames=[_blob(x, y) for x, y in positions],
        km_per_px=KM_PER_PX,
        location=CENTER,
        rain=52,
        storm=62,
        weak=40,
    )


def test_decode_radar_universal_blue() -> None:
    image = Image.new("RGBA", (4, 1), (0, 0, 0, 0))
    image.putpixel((0, 0), (0x00, 0xA3, 0xE0, 255))  # 20 dBZ
    image.putpixel((1, 0), (0xFF, 0x44, 0x00, 255))  # 45 dBZ
    image.putpixel((2, 0), (0x82, 0x7B, 0x69, 0x49))  # 0 dBZ, translucent
    assert decode_radar(image).tolist() == [[20, 45, 0, 0]]


def test_decode_infrared_cold_tops() -> None:
    image = Image.new("RGBA", (10, 1), (100, 100, 100, 255))  # warm gray
    image.putpixel((0, 0), (255, 230, 0, 255))  # -52 °C
    image.putpixel((1, 0), (0, 0, 115, 255))  # -30 °C: too warm, not in the palette
    image.putpixel((5, 0), (26, 0, 0, 255))  # -70 °C
    image.putpixel((6, 0), (129, 129, 129, 255))  # gray next to it: the core, -76 °C
    coldness = decode_infrared(image)[0]
    assert coldness[0] == 52
    assert coldness[1] == 0
    assert coldness[5] == 70
    assert coldness[6] == 75
    assert coldness[9] == 0  # gray far from any cold top: a warm cloud


def test_motion_of_a_moving_cell() -> None:
    # 2 px (4 km) every 10 min to the east and 1 px (2 km) to the south.
    field = _field([(20 + 2 * i, 40 + i) for i in range(6)])
    motion = estimate_motion(field)
    assert motion.speed_kmh == pytest.approx(26.8, abs=3)
    assert motion.toward_deg == pytest.approx(117, abs=8)
    assert motion.source == SOURCE_SATELLITE


def test_no_motion_without_echoes() -> None:
    field = _field([(0, 0)] * 4)
    field.frames = [np.zeros((SIZE, SIZE), dtype=np.float32)] * 4
    assert estimate_motion(field) is None


def test_arrival_of_an_approaching_cell() -> None:
    # Cell 30 km west of the location, moving east at 30 km/h.
    field = _field([(CENTER[0] - 15, CENTER[1])])
    nowcast = project_arrival(field, Motion(speed_kmh=30, toward_deg=90, source=SOURCE_SATELLITE))
    # The 6-px (12 km) cell edge is 18 km away; the "here" circle is 5 km + growth.
    assert T0 + timedelta(minutes=20) <= nowcast.arrival <= T0 + timedelta(minutes=40)
    assert nowcast.tipo == "lluvia"
    assert nowcast.from_deg == 270
    assert nowcast.until is not None and nowcast.until > nowcast.arrival


def test_storm_core_makes_it_a_storm() -> None:
    field = _field([(CENTER[0] - 15, CENTER[1])])
    field.frames[-1] *= 1.2  # 66: past the storm threshold
    nowcast = project_arrival(field, Motion(speed_kmh=30, toward_deg=90, source=SOURCE_SATELLITE))
    assert nowcast.tipo == "tormenta"


def test_cell_moving_away_never_arrives() -> None:
    field = _field([(CENTER[0] - 15, CENTER[1])])
    nowcast = project_arrival(field, Motion(speed_kmh=30, toward_deg=270, source=SOURCE_SATELLITE))
    assert nowcast.arrival is None


def test_raining_over_the_location_now() -> None:
    field = _field([CENTER])
    nowcast = project_arrival(field, None)
    assert nowcast.arrival == T0
    assert nowcast.distance_km == 0


def test_infrared_motion_disagreeing_with_the_wind_is_replaced() -> None:
    positions = [(20 + 2 * i, 40 + i) for i in range(6)]  # ~27 km/h to the ESE
    wind = Motion(speed_kmh=60, toward_deg=0, source=MOTION_WIND)
    assert run_nowcast(_field(positions), wind).motion is wind
    agreeing_wind = Motion(speed_kmh=35, toward_deg=100, source=MOTION_WIND)
    assert run_nowcast(_field(positions), agreeing_wind).motion.source == SOURCE_SATELLITE
    radar = _field(positions, source=SOURCE_RADAR)
    radar.frames = [Image.new("RGBA", (SIZE, SIZE))] * 6
    radar.decode = decode_radar
    assert run_nowcast(radar, wind).motion is wind  # no echoes: falls back
    assert agrees_with(Motion(30, 100, SOURCE_RADAR), Motion(40, 140, MOTION_WIND))
    assert not agrees_with(Motion(30, 100, SOURCE_RADAR), Motion(70, 100, MOTION_WIND))


def test_steering_motion_is_where_the_wind_blows_to() -> None:
    hourly = [
        {"when": T0 - timedelta(hours=1), "wind_speed_700hpa": 10, "wind_direction_700hpa": 0},
        {"when": T0, "wind_speed_700hpa": 50, "wind_direction_700hpa": 300},
        {"when": T0 + timedelta(hours=1), "wind_speed_700hpa": 99, "wind_direction_700hpa": 0},
    ]
    motion = steering_motion(hourly, T0 + timedelta(minutes=20))
    assert (motion.speed_kmh, motion.toward_deg, motion.source) == (50, 120, MOTION_WIND)
    assert steering_motion([], T0) is None


def test_aviso_area_carried_to_the_location() -> None:
    lat, lon = -34.6, -58.4
    # A 0.1° square ~20 km west of the location, moving east at 40 km/h.
    aviso = {
        "geometry": {
            "coordinates": [[[-58.70, -34.55], [-58.62, -34.55], [-58.62, -34.65], [-58.70, -34.65]]]
        }
    }
    arrival = aviso_arrival(lat, lon, aviso, T0, Motion(40, 90, MOTION_WIND))
    assert T0 + timedelta(minutes=15) <= arrival <= T0 + timedelta(minutes=30)
    assert aviso_arrival(lat, lon, aviso, T0, Motion(40, 270, MOTION_WIND)) is None


def test_sinarame_radars_nearest_first() -> None:
    from custom_components.smn_ar.nowcast import sinarame_radars_for

    assert sinarame_radars_for(-34.62, -58.43)[0] == "RMA2"  # Buenos Aires: Ezeiza
    assert sinarame_radars_for(-27.45, -58.98)[0] == "RMA4"  # Resistencia
    assert sinarame_radars_for(-60.0, -45.0) == []


def _sinarame_image(color: tuple[int, int, int] | None, fill: float = 0.1) -> Image.Image:
    """512 px radar image: a block of one legend color, the rest transparent."""
    image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    if color is not None:
        side = int(512 * fill**0.5)
        image.paste((*color, 255), (100, 100, 100 + side, 100 + side))
    return image


def test_sinarame_decoder() -> None:
    from custom_components.smn_ar.nowcast import _sinarame_decoder

    decode = _sinarame_decoder("RMA2", (0, 0))
    rain = decode((_sinarame_image((0x4C, 0xE1, 0x32)), None))  # legend's 20 dBZ
    assert rain[150, 150] == 20
    assert rain[400, 400] == 0
    # A ring-artifact scan: most of the image at 55 dBZ or more.
    assert decode((_sinarame_image((0xE2, 0xF5, 0xEE), fill=0.5), None)) is None


def test_sinarame_needs_clouds_for_rain_and_cold_tops_for_storms() -> None:
    from custom_components.smn_ar.nowcast import SINARAME_RADARS, _deg2pixel, _sinarame_decoder

    _, south, west, north, east = SINARAME_RADARS["RMA2"]
    # Infrared mosaic (GIBS zoom 6) starting a bit NW of the radar's square.
    x0, y0 = _deg2pixel(north, west, 6)
    origin = (int(x0) - 10, int(y0) - 10)
    decode = _sinarame_decoder("RMA2", origin)
    storm = _sinarame_image((0xC5, 0x00, 0x17))  # 45 dBZ

    def infrared(color: tuple[int, int, int]) -> Image.Image:
        return Image.new("RGBA", (300, 300), (*color, 255))

    rain = _sinarame_image((0x3A, 0xB0, 0x27))  # 25 dBZ
    assert decode((storm, infrared((100, 100, 100)))).max() == 0  # clear: warm ground
    assert decode((rain, infrared((190, 190, 190)))).max() == 25  # about -16 °C: rain
    assert decode((storm, infrared((190, 190, 190)))).max() == 0  # no storm tops: terrain
    assert decode((storm, infrared((255, 0, 0)))).max() == 45  # -61 °C


def test_sinarame_radars_in_view_nearest_last() -> None:
    from custom_components.smn_ar.nowcast import sinarame_radars_in_view

    radars = sinarame_radars_in_view(-36.0, -60.0, -33.5, -57.0)  # around Buenos Aires
    assert radars[-1] == "RMA2"
    assert "RMA14" in radars
    assert sinarame_radars_in_view(-60.0, -45.0, -59.0, -44.0) == []


def test_colorize_dbz_like_rainviewer() -> None:
    from custom_components.smn_ar.nowcast import colorize_dbz, decode_radar

    dbz = np.array([[0, 10, 20, 45]], dtype=np.float32)
    image = colorize_dbz(dbz)
    assert [image.getpixel((x, 0))[3] for x in range(4)] == [0, 0, 255, 255]
    assert decode_radar(image).tolist() == [[0, 0, 20, 45]]


def test_clean_radar_frames() -> None:
    from custom_components.smn_ar.nowcast import clean_radar_frames

    def frame(cells: list[tuple[int, int]], extra: int = 0) -> np.ndarray:
        f = np.zeros((100, 100), dtype=np.float32)
        f[0:3, 0:3] = 30  # a clutter spot, in every frame
        for x, y in cells:
            f[y : y + 20, x : x + 20 + extra] = 30
        return f

    full = [frame([(10 * i, 40), (60, 10)], extra=10) for i in range(4)]
    sparse = frame([])
    frames = [full[0], sparse, full[1], full[2], sparse, full[3]]
    keep = clean_radar_frames(frames, 15)
    assert keep == [0, 2, 3, 5]
    # Rain fading out over time isn't a sparse scan.
    fading = [frame([(10, 10)], extra=60 - 15 * i) for i in range(4)]
    assert clean_radar_frames(fading, 15) == [0, 1, 2, 3]
    assert frames[0][1, 1] == 0  # static clutter gone
    assert frames[0][45, 5] == 30  # moving rain kept


def test_clutter_maps_learn_terrain_and_persist(tmp_path) -> None:
    from custom_components.smn_ar.nowcast import ClutterMaps

    clutter = ClutterMaps()
    terrain = np.zeros((10, 10), dtype=bool)
    terrain[2, 2] = True
    for i in range(40):
        echoes = terrain.copy()
        echoes[5, i % 10] = True  # rain passing through
        clutter.observe("RMA1", T0 + timedelta(minutes=10 * i), echoes)
        if i < 30:
            assert clutter.mask("RMA1") is None  # not enough images yet
    clutter.observe("RMA1", T0, terrain)  # an image already seen: ignored
    mask = clutter.mask("RMA1")
    assert mask[2, 2] and not mask[5, 3]

    path = str(tmp_path / "clutter.npz")
    clutter.save(path)
    restored = ClutterMaps()
    restored.load(path)
    assert restored.mask("RMA1")[2, 2]
    ClutterMaps().load(str(tmp_path / "missing.npz"))  # no file yet: fine
