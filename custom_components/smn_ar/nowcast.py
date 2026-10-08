"""Nowcast: when the rain or storms around the location get there, 0-2h ahead.

Plain extrapolation, the same idea as a radar app's "rain in 25 min": take
the last frames over a square area around the location, find how far the
echoes moved between them (one motion vector for the whole area, by cross-
correlation), and slide the latest frame along that vector until something
reaches the location.

Three sources, each on its own grid, the first one available wins:
- Argentina's own radar network (SINARAME), one radar's image at a time,
  from the national water resources office's public viewer — see
  fetch_sinarame_field for its quirks (and the artifacts it filters out).
- RainViewer's radar (reflectivity in dBZ), only where RainViewer has radar
  coverage, which in Argentina is little more than the Uruguay river (from
  Brazil's and Uruguay's radars) — see radar_covers.
- GOES-East's Band13 infrared from NASA GIBS (cloud-top temperature), all
  over the country. Cold tops mean deep convection, so this sees storms,
  but not the stratiform rain under warmer clouds (that's left to the
  forecasts) — and an anvil spreads well past where it actually rains.

When the frames don't give a motion (too few echoes, or they changed too
much), the 700 hPa wind is the fallback: it's the steering flow storms
move with, unlike the surface wind SMN forecasts.

numpy is imported lazily: it ships with Home Assistant (like PyAV, see
satellite.encode_mp4), and without it there's just no nowcast.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
from datetime import datetime, timedelta, timezone
import logging
import math
import os
from typing import Any, Callable

import aiohttp
import async_timeout

from .alert_levels import distance_to_aviso_km
from .const import (
    GIBS_LAYER_INFRARED,
    GIBS_MATRIX_SET_INFRARED,
    GIBS_MAX_ZOOM_INFRARED,
    GIBS_TILE_URL_TEMPLATE,
    NOWCAST_AREA_RADIUS_KM,
    NOWCAST_HORIZON_MINUTES,
    NOWCAST_LOCATION_RADIUS_KM,
    NOWCAST_MAX_AGE_MINUTES,
    RADAR_TILE_SIZE,
    RAINVIEWER_MAX_ZOOM,
)
from .radar import _deg2pixel, _fetch_frame_paths, _fetch_tile, _tile_semaphore

_LOGGER = logging.getLogger(__name__)

_EARTH_CIRCUMFERENCE_KM = 40075.016686
_STEP_MINUTES = 5
_MAX_SPEED_KMH = 130
# Frame pairs compared for the motion: farther apart, a pixel is a smaller
# speed step; too far, the echoes have changed.
_MIN_MOTION_GAP_MINUTES = 15
_MAX_MOTION_GAP_MINUTES = 50
_MOTION_TOLERANCE_KMH = 12
_MOTION_TOLERANCE_RATIO = 0.3
_STRONG_STEERING_KMH = 30
_MIN_ACTIVE_PIXELS = 12
_MIN_MOTION_SCORE = 0.3
_MAX_MOTION_GRID = 160  # px; bigger fields are block-averaged down first
# The "here" circle grows with the lead time: the motion is never exact.
_RADIUS_GROWTH = 0.1  # km per km travelled
_MAX_LOCATION_RADIUS_KM = 15

# GIBS publishes the infrared 10-40 min late: look back this far for frames.
_IR_LOOKBACK_SLOTS = 8  # 10-min slots
_RADAR_FRAMES = 4  # RainViewer's last 30 min

# RainViewer's "Universal Blue" scheme (the only one its free API serves
# now), from 15 dBZ up — per rainviewer.com/api/color-schemes.html. Below
# that it's translucent drizzle/noise, read as no rain. Fetched with
# smoothing off ("0_0"), so tiles carry these exact colors.
_RADAR_PALETTE_START_DBZ = 15
_RADAR_PALETTE = (
    "88ddee 6cd1eb 51c5e8 36bae5 1baee2 00a3e0 009ad5 0091ca 0088bf 007fb4 "
    "0077aa 0070a3 00699c 006295 005b8e 005588 005180 004e78 004a70 004768 "
    "ffee00 ffe000 ffd200 ffc500 ffb700 ffaa00 ff9f00 ff9500 ff8b00 ff8100 "
    "ff4400 f23600 e62800 d91b00 cd0d00 c10000 a80000 8f0000 760000 5d0000 "
    "ffaaff ff9fff ff95ff ff8bff ff81ff ff77ff ff6cff ff62ff ff58ff ff4eff ffffff"
).split()
RADAR_RAIN_DBZ = 20  # light rain
RADAR_STORM_DBZ = 45
_RADAR_WEAK_DBZ = 15

# GIBS' Band13 colormap (Clean_Longwave_Infrared_Window_Band.xml), its
# colored part: -91..-81 °C purples, then -70..-31 °C reds/yellows/greens.
# -80..-71 °C (and -92) are grays the warm clouds also use, so they aren't
# looked up: a gray pixel next to a very cold one is taken as the core of
# the same storm instead (see decode_infrared).
_IR_PALETTE = [
    (-91, (127, 0, 127)), (-90, (140, 13, 135)), (-89, (153, 25, 142)), (-88, (165, 38, 150)),
    (-87, (178, 51, 157)), (-86, (191, 64, 165)), (-85, (204, 76, 173)), (-84, (217, 89, 180)),
    (-83, (229, 102, 188)), (-82, (242, 114, 195)), (-81, (255, 127, 203)),
    (-70, (26, 0, 0)), (-69, (51, 0, 0)), (-68, (77, 0, 0)), (-67, (102, 0, 0)),
    (-66, (128, 0, 0)), (-65, (153, 0, 0)), (-64, (179, 0, 0)), (-63, (204, 0, 0)),
    (-62, (230, 0, 0)), (-61, (255, 0, 0)), (-60, (255, 26, 0)), (-59, (255, 51, 0)),
    (-58, (255, 77, 0)), (-57, (255, 102, 0)), (-56, (255, 128, 0)), (-55, (255, 153, 0)),
    (-54, (255, 179, 0)), (-53, (255, 204, 0)), (-52, (255, 230, 0)), (-51, (255, 255, 0)),
    (-50, (230, 255, 0)), (-49, (204, 255, 0)), (-48, (179, 255, 0)), (-47, (153, 255, 0)),
    (-46, (128, 255, 0)), (-45, (102, 255, 0)), (-44, (77, 255, 0)), (-43, (51, 255, 0)),
    (-42, (26, 255, 0)), (-41, (0, 255, 0)), (-40, (0, 234, 10)), (-39, (0, 212, 19)),
    (-38, (0, 191, 29)), (-37, (0, 170, 38)), (-36, (0, 149, 48)), (-35, (0, 128, 58)),
    (-34, (0, 106, 67)), (-33, (0, 85, 77)), (-32, (0, 64, 86)), (-31, (0, 42, 96)),
]
# Infrared fields are "coldness": degrees below 0 °C of the cloud top, so
# that, like dBZ, more is more rain.
IR_RAIN_COLDNESS = 52  # tops at -52 °C or colder: deep convection, raining under it
IR_STORM_COLDNESS = 62
_IR_WEAK_COLDNESS = 40
_IR_GRAY_CORE_COLDNESS = 75
_IR_CORE_NEIGHBOR_COLDNESS = 65

SOURCE_RADAR = "radar"
SOURCE_SINARAME = "radar SINARAME"
SOURCE_SATELLITE = "satélite infrarrojo"
MOTION_WIND = "viento en 700 hPa"


@dataclass
class Field:
    """One source's frames over a square area, on one grid (oldest first)."""

    source: str
    times: list[datetime]  # UTC
    # The fetched frames, until run_nowcast decodes them (with `decode`)
    # into 2D numpy arrays: dBZ, or coldness for the infrared. A frame
    # `decode` returns None for is unusable and dropped. No `decode`: the
    # frames are arrays already.
    frames: list[Any]
    km_per_px: float
    location: tuple[float, float]  # the configured location, in pixels (x east, y south)
    rain: float
    storm: float
    weak: float
    decode: Callable[[Any], Any] | None = None
    # Pixels left out of the motion (2D bool array), e.g. a radar's own
    # ground clutter: it doesn't move, so it would pin the motion at zero.
    motion_mask: Any | None = None
    # Run clean_radar_frames on the decoded frames (SINARAME's).
    clean: bool = False


@dataclass
class Motion:
    """Where the echoes are heading: speed and the bearing they move towards."""

    speed_kmh: float
    toward_deg: float
    source: str  # SOURCE_* when measured from the frames, MOTION_WIND otherwise

    @property
    def east_north_kmh(self) -> tuple[float, float]:
        rad = math.radians(self.toward_deg)
        return self.speed_kmh * math.sin(rad), self.speed_kmh * math.cos(rad)


@dataclass
class Nowcast:
    """One source's verdict for the location.

    `arrival` is None when nothing reaches the location before
    `horizon_end`. With rain over the location in the latest frame,
    `arrival` is that frame's time (it's under way).
    """

    def is_current(self, now: datetime) -> bool:
        """Whether it still says something about `now` on.

        Not when its horizon is over, its frames are too old (the source
        has been failing: the coordinator keeps the last result), or the
        rain it saw has already gone past.
        """
        if now >= self.horizon_end or now - self.frame_time > timedelta(minutes=NOWCAST_MAX_AGE_MINUTES):
            return False
        return not (self.until is not None and self.until <= now)

    source: str
    frame_time: datetime  # UTC, the latest frame
    horizon_end: datetime
    motion: Motion | None
    arrival: datetime | None = None
    until: datetime | None = None  # when it's past the location, if within the horizon
    tipo: str | None = None
    intensity: float | None = None  # dBZ, or the cloud-top temperature in °C
    distance_km: float | None = None  # how far the incoming echo is in the latest frame
    from_deg: float | None = None  # bearing from the location to it


def km_per_pixel(latitude: float, zoom: int) -> float:
    """Web Mercator pixel size at a latitude and zoom."""
    return _EARTH_CIRCUMFERENCE_KM * math.cos(math.radians(latitude)) / (RADAR_TILE_SIZE * 2**zoom)


def _palette_lookup(rgb: Any, colors: list[tuple[int, int, int]], values: list[float], max_distance: float) -> Any:
    """Each pixel's value from its nearest palette color; 0 when none is close.

    Only the image's distinct colors are matched, a few dozen at most.
    """
    import numpy as np

    packed = (rgb[..., 0].astype(np.int32) << 16) | (rgb[..., 1].astype(np.int32) << 8) | rgb[..., 2]
    unique, inverse = np.unique(packed, return_inverse=True)
    unique_rgb = np.stack([(unique >> 16) & 255, (unique >> 8) & 255, unique & 255], axis=1)
    palette = np.array(colors, dtype=np.int32)
    distances = ((unique_rgb[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2)
    nearest = distances.argmin(axis=1)
    matched = np.where(
        distances[np.arange(len(unique)), nearest] <= max_distance**2,
        np.array(values, dtype=np.float32)[nearest],
        0,
    )
    return matched[inverse].reshape(packed.shape).astype(np.float32)


def decode_radar(image: Any) -> Any:
    """RainViewer Universal Blue RGBA tile mosaic -> dBZ (0 where there's no rain)."""
    import numpy as np

    pixels = np.asarray(image.convert("RGBA"))
    colors = [tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) for h in _RADAR_PALETTE]
    values = [_RADAR_PALETTE_START_DBZ + i for i in range(len(colors))]
    dbz = _palette_lookup(pixels[..., :3], colors, values, max_distance=24)
    dbz[pixels[..., 3] < 255] = 0
    return dbz


def decode_infrared(image: Any) -> Any:
    """GIBS Band13 RGBA -> coldness (°C below zero of the cloud tops, 0 if warmer than -31 °C)."""
    import numpy as np

    pixels = np.asarray(image.convert("RGBA")).astype(np.int32)
    rgb = pixels[..., :3]
    coldness = _palette_lookup(rgb, [c for _, c in _IR_PALETTE], [-t for t, _ in _IR_PALETTE], max_distance=24)
    gray = (np.abs(rgb[..., 0] - rgb[..., 1]) < 8) & (np.abs(rgb[..., 1] - rgb[..., 2]) < 8)
    coldness[gray] = 0
    # A gray (-71..-80 °C, or -92) core inside a very cold storm top.
    very_cold = coldness >= _IR_CORE_NEIGHBOR_COLDNESS
    near_very_cold = np.zeros_like(very_cold)
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            near_very_cold |= np.roll(np.roll(very_cold, dy, axis=0), dx, axis=1)
    coldness[gray & near_very_cold] = _IR_GRAY_CORE_COLDNESS
    coldness[pixels[..., 3] == 0] = 0
    return coldness


def _downsample(array: Any, factor: int) -> Any:
    if factor <= 1:
        return array
    h, w = (array.shape[0] // factor) * factor, (array.shape[1] // factor) * factor
    return array[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))


def _shifted_overlap(a: Any, b: Any, dx: int, dy: int) -> tuple[Any, Any]:
    """(a's part, b's part) where a moved by (dx, dy) overlaps b."""
    h, w = a.shape
    a_part = a[max(-dy, 0) : h - max(dy, 0), max(-dx, 0) : w - max(dx, 0)]
    b_part = b[max(dy, 0) : h - max(-dy, 0), max(dx, 0) : w - max(-dx, 0)]
    return a_part, b_part


def _subpixel(minus: float, center: float, plus: float) -> float:
    """Peak offset from a parabola through three neighboring scores."""
    denominator = minus - 2 * center + plus
    return 0.0 if denominator >= 0 else max(-0.5, min(0.5, 0.5 * (minus - plus) / denominator))


def _pair_motion(field: Field, older: int) -> tuple[float, float] | None:
    """(east, north) km/h of the echoes from frame `older` to the latest, by cross-correlation.

    Zero-mean (Pearson) correlation, so a uniform cloud shield doesn't just
    match itself unshifted. None when there's too little to track or no
    shift matches well enough (the echoes changed too much).
    """
    import numpy as np

    hours = (field.times[-1] - field.times[older]).total_seconds() / 3600
    factor = max(1, math.ceil(max(field.frames[-1].shape) / _MAX_MOTION_GRID))
    a = np.clip(field.frames[older] - field.weak, 0, None)
    b = np.clip(field.frames[-1] - field.weak, 0, None)
    if field.motion_mask is not None:
        a, b = np.where(field.motion_mask, 0, a), np.where(field.motion_mask, 0, b)
    a, b = _downsample(a, factor), _downsample(b, factor)
    if (a > 0).sum() < _MIN_ACTIVE_PIXELS or (b > 0).sum() < _MIN_ACTIVE_PIXELS:
        return None
    a, b = a - a.mean(), b - b.mean()
    norm = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
    if norm == 0:
        return None
    km_per_px = field.km_per_px * factor
    max_shift = math.ceil(_MAX_SPEED_KMH * hours / km_per_px)

    scores: dict[tuple[int, int], float] = {}
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            if dx * dx + dy * dy > max_shift * max_shift:
                continue
            a_part, b_part = _shifted_overlap(a, b, dx, dy)
            scores[(dx, dy)] = float((a_part * b_part).sum()) / norm
    (dx, dy), score = max(scores.items(), key=lambda item: item[1])
    if score < _MIN_MOTION_SCORE:
        return None
    fx = dx + _subpixel(scores.get((dx - 1, dy), score), score, scores.get((dx + 1, dy), score))
    fy = dy + _subpixel(scores.get((dx, dy - 1), score), score, scores.get((dx, dy + 1), score))
    return fx * km_per_px / hours, -fy * km_per_px / hours


def _motion_from(east: float, north: float, source: str) -> Motion:
    return Motion(
        speed_kmh=round(math.hypot(east, north), 1),
        toward_deg=round(math.degrees(math.atan2(east, north)) % 360),
        source=source,
    )


def estimate_motion(field: Field) -> Motion | None:
    """The area's echo motion, agreed on by the frame pairs 15-50 min apart.

    Each older frame against the latest gives one estimate; most of them
    must agree (within _MOTION_TOLERANCE of their median), and the motion
    is the mean of those. None otherwise: the echoes are growing/decaying
    more than moving, and any single estimate is a coin toss.
    """
    vectors = []
    for older, when in enumerate(field.times[:-1]):
        gap = (field.times[-1] - when).total_seconds() / 60
        if _MIN_MOTION_GAP_MINUTES <= gap <= _MAX_MOTION_GAP_MINUTES:
            vector = _pair_motion(field, older)
            if vector is not None:
                vectors.append(vector)
    if not vectors:
        return None
    east_median = sorted(e for e, _ in vectors)[len(vectors) // 2]
    north_median = sorted(n for _, n in vectors)[len(vectors) // 2]
    tolerance = max(_MOTION_TOLERANCE_KMH, _MOTION_TOLERANCE_RATIO * math.hypot(east_median, north_median))
    agreeing = [(e, n) for e, n in vectors if math.hypot(e - east_median, n - north_median) <= tolerance]
    if len(agreeing) * 2 <= len(vectors) and len(vectors) > 1:
        return None
    return _motion_from(
        sum(e for e, _ in agreeing) / len(agreeing), sum(n for _, n in agreeing) / len(agreeing), field.source
    )


def agrees_with(motion: Motion, reference: Motion) -> bool:
    """Whether two motions roughly match: within 60° and a factor of 2 in speed.

    Slow motions (either under _MOTION_TOLERANCE_KMH) agree on speed alone,
    their direction being mostly noise.
    """
    if max(motion.speed_kmh, reference.speed_kmh) < _MOTION_TOLERANCE_KMH:
        return True
    if min(motion.speed_kmh, reference.speed_kmh) * 2 < max(motion.speed_kmh, reference.speed_kmh):
        return False
    difference = abs((motion.toward_deg - reference.toward_deg + 180) % 360 - 180)
    return difference <= 60


def _window_peak(frame: Any, x: float, y: float, radius_px: float) -> float:
    """Highest value within radius_px of (x, y); 0 outside the frame."""
    import numpy as np

    h, w = frame.shape
    x0, x1 = max(0, math.floor(x - radius_px)), min(w, math.ceil(x + radius_px) + 1)
    y0, y1 = max(0, math.floor(y - radius_px)), min(h, math.ceil(y + radius_px) + 1)
    if x0 >= x1 or y0 >= y1:
        return 0.0
    ys, xs = np.ogrid[y0:y1, x0:x1]
    inside = (xs - x) ** 2 + (ys - y) ** 2 <= radius_px**2
    window = frame[y0:y1, x0:x1][inside]
    return float(window.max()) if window.size else 0.0


def project_arrival(field: Field, motion: Motion | None, horizon_minutes: int = NOWCAST_HORIZON_MINUTES) -> Nowcast:
    """Slide the latest frame along the motion until rain reaches the location.

    Looking backwards: what will be over the location in τ minutes is what's
    now upstream, τ·speed away against the motion. Without a motion, only
    whether it's raining over the location right now.
    """
    frame = field.frames[-1]
    frame_time = field.times[-1]
    nowcast = Nowcast(
        source=field.source,
        frame_time=frame_time,
        horizon_end=frame_time + timedelta(minutes=horizon_minutes),
        motion=motion,
    )
    loc_x, loc_y = field.location
    east, north = motion.east_north_kmh if motion else (0.0, 0.0)
    leads = range(0, horizon_minutes + 1, _STEP_MINUTES) if motion else range(1)
    for lead in leads:
        travelled_km = math.hypot(east, north) * lead / 60
        x = loc_x - east * lead / 60 / field.km_per_px
        y = loc_y + north * lead / 60 / field.km_per_px
        radius_km = min(NOWCAST_LOCATION_RADIUS_KM + _RADIUS_GROWTH * travelled_km, _MAX_LOCATION_RADIUS_KM)
        peak = _window_peak(frame, x, y, radius_km / field.km_per_px)
        if nowcast.arrival is None:
            if peak < field.rain:
                continue
            nowcast.arrival = frame_time + timedelta(minutes=lead)
            nowcast.intensity = -peak if field.source == SOURCE_SATELLITE else peak
            nowcast.tipo = "tormenta" if peak >= field.storm else "lluvia"
            nowcast.distance_km = round(travelled_km, 1)
            nowcast.from_deg = None if lead == 0 else round((motion.toward_deg + 180) % 360)
        elif peak < field.rain:
            nowcast.until = frame_time + timedelta(minutes=lead)
            break
        elif peak >= field.storm:
            nowcast.tipo = "tormenta"
    return nowcast


_SPARSE_SCAN_RATIO = 0.5
_SPARSE_SCAN_MIN_PIXELS = 1000
_STATIC_CLUTTER_SHARE = 0.9
_STATIC_CLUTTER_MIN_FRAMES = 4


def clean_radar_frames(frames: list[Any], weak: float) -> list[int]:
    """Drop a radar's sparse scans and its static clutter; the indices of the frames kept.

    - A frame with under half the echo area of the frames on both sides
      is the other scan type some radars interleave (seen live on
      Córdoba's): left out, or the animation flickers and the motion
      jumps. Against its neighbors, not the fullest frame, so rain that's
      fading out isn't mistaken for it.
    - A pixel with echoes in nearly every frame is terrain, not rain (the
      sierras around Córdoba): zeroed in place, in all of them. Rain that
      sits on the very same pixel for the whole window is rare; this only
      runs with enough frames to tell.
    """
    import numpy as np

    areas = [int((frame >= weak).sum()) for frame in frames]
    keep = []
    for i, area in enumerate(areas):
        neighbors = areas[max(i - 1, 0) : i] + areas[i + 1 : i + 2]
        reference = min(neighbors, default=0)
        if reference < _SPARSE_SCAN_MIN_PIXELS or area >= _SPARSE_SCAN_RATIO * reference:
            keep.append(i)
    kept = [frames[i] for i in keep]
    if len(kept) >= _STATIC_CLUTTER_MIN_FRAMES:
        share = np.mean([frame >= weak for frame in kept], axis=0)
        static = share >= _STATIC_CLUTTER_SHARE
        for frame in kept:
            frame[static] = 0
    return keep


def run_nowcast(field: Field, steering: Motion | None) -> Nowcast | None:
    """Decode the fetched frames, then their motion and the arrival.

    The steering wind stands in when the frames give no motion, when a
    radar's echoes barely move under a strong steering flow — and for
    the infrared, also when they disagree with it: the cold tops of a big
    system follow its anvil spreading out, not where it rains (seen live:
    infrared 25 km/h to the N, radar 50 km/h to the E, 700 hPa wind 70
    km/h to the SE).

    None when no frame survives decoding (see Field.decode).

    CPU only, run it in an executor.
    """
    if field.decode is not None:
        decoded = [(when, field.decode(frame)) for when, frame in zip(field.times, field.frames)]
        decoded = [(when, frame) for when, frame in decoded if frame is not None]
        if not decoded:
            return None
        field.times = [when for when, _ in decoded]
        field.frames = [frame for _, frame in decoded]
    if field.clean:
        keep = clean_radar_frames(field.frames, field.weak)
        field.times = [field.times[i] for i in keep]
        field.frames = [field.frames[i] for i in keep]
    motion = estimate_motion(field)
    if motion is None or (
        steering is not None
        and (
            (field.source == SOURCE_SATELLITE and not agrees_with(motion, steering))
            # A radar's echoes standing still under a strong steering flow:
            # what it tracked is clutter or interference, not rain.
            or (motion.speed_kmh < _MOTION_TOLERANCE_KMH and steering.speed_kmh >= _STRONG_STEERING_KMH)
        )
    ):
        motion = steering
    return project_arrival(field, motion)


def steering_motion(hourly: list[dict[str, Any]] | None, now: datetime | None = None) -> Motion | None:
    """The 700 hPa wind for the current hour (Open-Meteo), as a storm motion."""
    now = now or datetime.now(timezone.utc)
    current = None
    for hour in hourly or []:
        if hour["when"] <= now:
            current = hour
        else:
            break
    if not current or current.get("wind_speed_700hpa") is None or current.get("wind_direction_700hpa") is None:
        return None
    # Wind direction is where it blows from.
    return Motion(
        speed_kmh=float(current["wind_speed_700hpa"]),
        toward_deg=(float(current["wind_direction_700hpa"]) + 180) % 360,
        source=MOTION_WIND,
    )


def aviso_arrival(
    latitude: float,
    longitude: float,
    aviso: dict[str, Any],
    issued: datetime,
    motion: Motion,
    horizon_minutes: int = NOWCAST_HORIZON_MINUTES,
) -> datetime | None:
    """When an aviso's area, carried along by `motion` from its issue time, reaches the location."""
    east, north = motion.east_north_kmh
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(latitude))
    for lead in range(0, horizon_minutes + 1, _STEP_MINUTES):
        # Same as moving the polygon forwards: move the location backwards.
        lat = latitude - north * lead / 60 / km_per_deg_lat
        lon = longitude - east * lead / 60 / km_per_deg_lon
        result = distance_to_aviso_km(lat, lon, aviso)
        if result and result[0] <= NOWCAST_LOCATION_RADIUS_KM:
            return issued + timedelta(minutes=lead)
    return None


# --- Fetching ---------------------------------------------------------------

_COVERAGE_URL = "https://tilecache.rainviewer.com/v2/coverage/0/256/{z}/{x}/{y}/0/0_0.png"
_coverage_cache: dict[tuple[float, float], bool] = {}


async def radar_covers(session: aiohttp.ClientSession, latitude: float, longitude: float) -> bool:
    """Whether RainViewer has radar over the location (its coverage layer, transparent where covered).

    Cached for the run: the radar network doesn't change from one hour to
    the next. A failed fetch comes back fully transparent, so it reads as
    covered — the radar is tried, and an empty radar just finds nothing.
    """
    key = (round(latitude, 2), round(longitude, 2))
    if key not in _coverage_cache:
        x, y = _deg2pixel(latitude, longitude, RAINVIEWER_MAX_ZOOM)
        tile = await _fetch_tile(
            session,
            _COVERAGE_URL.format(
                z=RAINVIEWER_MAX_ZOOM, x=int(x // RADAR_TILE_SIZE), y=int(y // RADAR_TILE_SIZE)
            ),
        )
        alpha = tile.getpixel((int(x % RADAR_TILE_SIZE), int(y % RADAR_TILE_SIZE)))[3]
        _coverage_cache[key] = alpha == 0
    return _coverage_cache[key]


async def _fetch_area(
    session: aiohttp.ClientSession,
    url_for_tile: Any,
    zoom: int,
    latitude: float,
    longitude: float,
    radius_km: float = NOWCAST_AREA_RADIUS_KM,
) -> tuple[Any, tuple[float, float], bool]:
    """(square mosaic radius_km around a point, the point's pixel in it, complete).

    Not complete when a tile came back fully transparent: _fetch_tile's
    answer to a failed request (only meaningful for always-opaque layers).
    """
    from PIL import Image

    half = math.ceil(radius_km / km_per_pixel(latitude, zoom))
    cx, cy = _deg2pixel(latitude, longitude, zoom)
    left, top = round(cx) - half, round(cy) - half
    tx0, tx1 = left // RADAR_TILE_SIZE, (left + 2 * half) // RADAR_TILE_SIZE
    ty0, ty1 = top // RADAR_TILE_SIZE, (top + 2 * half) // RADAR_TILE_SIZE
    positions = [(tx, ty) for tx in range(tx0, tx1 + 1) for ty in range(ty0, ty1 + 1)]
    tiles = await asyncio.gather(*(_fetch_tile(session, url_for_tile(zoom, tx, ty)) for tx, ty in positions))
    raw = Image.new("RGBA", ((tx1 - tx0 + 1) * RADAR_TILE_SIZE, (ty1 - ty0 + 1) * RADAR_TILE_SIZE))
    for (tx, ty), tile in zip(positions, tiles):
        raw.paste(tile, ((tx - tx0) * RADAR_TILE_SIZE, (ty - ty0) * RADAR_TILE_SIZE))
    ox, oy = left - tx0 * RADAR_TILE_SIZE, top - ty0 * RADAR_TILE_SIZE
    mosaic = raw.crop((ox, oy, ox + 2 * half, oy + 2 * half))
    complete = all(tile.getchannel("A").getbbox() for tile in tiles)
    return mosaic, (cx - left, cy - top), complete


async def fetch_infrared_field(
    session: aiohttp.ClientSession, latitude: float, longitude: float, now: datetime | None = None
) -> Field | None:
    """GIBS Band13 frames of the last ~80 min that are already published."""
    now = now or datetime.now(timezone.utc)
    latest_slot = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    times = [latest_slot - timedelta(minutes=10 * i) for i in reversed(range(_IR_LOOKBACK_SLOTS))]

    def url_for(when: datetime) -> Any:
        time_str = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        return lambda z, x, y: GIBS_TILE_URL_TEMPLATE.format(
            layer=GIBS_LAYER_INFRARED, time=time_str, matrix_set=GIBS_MATRIX_SET_INFRARED, z=z, x=x, y=y
        )

    results = await asyncio.gather(
        *(_fetch_area(session, url_for(when), GIBS_MAX_ZOOM_INFRARED, latitude, longitude) for when in times)
    )
    available = [(when, mosaic, location) for when, (mosaic, location, complete) in zip(times, results) if complete]
    if not available:
        return None
    return Field(
        source=SOURCE_SATELLITE,
        times=[when for when, _, _ in available],
        frames=[mosaic for _, mosaic, _ in available],
        km_per_px=km_per_pixel(latitude, GIBS_MAX_ZOOM_INFRARED),
        location=available[0][2],
        rain=IR_RAIN_COLDNESS,
        storm=IR_STORM_COLDNESS,
        weak=_IR_WEAK_COLDNESS,
        decode=decode_infrared,
    )


async def fetch_radar_field(session: aiohttp.ClientSession, latitude: float, longitude: float) -> Field | None:
    """RainViewer's last frames, at its own max zoom and without smoothing."""
    host, paths = await _fetch_frame_paths(session)
    paths = paths[-_RADAR_FRAMES:]
    if not paths:
        return None

    def url_for(path: str) -> Any:
        return lambda z, x, y: f"{host}{path}/{RADAR_TILE_SIZE}/{z}/{x}/{y}/2/0_0.png"

    results = await asyncio.gather(
        *(_fetch_area(session, url_for(path), RAINVIEWER_MAX_ZOOM, latitude, longitude) for _, path in paths)
    )
    # Radar tiles are transparent where it isn't raining: an "incomplete"
    # mosaic is just a dry one, so every frame counts.
    return Field(
        source=SOURCE_RADAR,
        times=[when for when, _ in paths],
        frames=[mosaic for mosaic, _, _ in results],
        km_per_px=km_per_pixel(latitude, RAINVIEWER_MAX_ZOOM),
        location=results[0][1],
        rain=RADAR_RAIN_DBZ,
        storm=RADAR_STORM_DBZ,
        weak=_RADAR_WEAK_DBZ,
        decode=decode_radar,
    )


# --- SINARAME -----------------------------------------------------------------

# Public viewer of the national weather radar network (SINARAME), run by
# the water resources office (radares.hidricosargentina.gob.ar, no robots
# rules, no bot challenge). Its map overlays one PNG per radar and frame;
# the file names are Argentina time, every 10 min, published ~25 min late
# and with gaps.
SINARAME_URL = "https://radares.hidricosargentina.gob.ar/cache/{radar}/{stamp}.png"
SINARAME_ATTRIBUTION = "Radares: SINARAME (radares.hidricosargentina.gob.ar)"
_ARG_TZ = timezone(timedelta(hours=-3))
# (name, south, west, north, east): each image is 512 px over a square
# 240 km around the radar, placed on the map (Leaflet, Web Mercator) with
# these bounds. RMA19 (Santa Rosa) and RMA21 (Tostado) weren't publishing
# when these were taken, so they're missing.
SINARAME_RADARS: dict[str, tuple[str, float, float, float, float]] = {
    "RMA1": ("Córdoba", -33.6118, -66.719, -29.2708, -61.6651),
    "RMA2": ("Ezeiza", -36.9713, -61.1411, -32.6303, -55.89),
    "RMA3": ("Las Lomitas", -26.9008, -62.925, -22.5598, -58.1778),
    "RMA4": ("Resistencia", -29.6223, -61.4806, -25.2813, -56.6215),
    "RMA5": ("Bernardo de Irigoyen", -28.4486, -56.0752, -24.1076, -51.2664),
    "RMA6": ("Mar del Plata", -40.0836, -60.2605, -35.7426, -54.7951),
    "RMA7": ("Neuquén", -41.0471, -70.9143, -36.7061, -65.3755),
    "RMA8": ("Mercedes", -31.3664, -60.5145, -27.0254, -55.5751),
    "RMA9": ("Río Grande", -55.9545, -71.3933, -51.6135, -64.0953),
    "RMA10": ("Bahía Blanca", -40.9048, -64.9272, -36.5638, -59.3996),
    "RMA11": ("Termas de Río Hondo", -29.6731, -67.3364, -25.3321, -62.4752),
    "RMA12": ("Las Grutas", -42.9427, -67.923, -38.6017, -62.2293),
    "RMA13": ("Ituzaingó", -29.7929, -59.275, -25.4519, -54.4084),
    "RMA14": ("Bolívar", -38.3597, -63.7415, -34.0187, -58.3989),
    "RMA15": ("Patquía", -32.2013, -69.3663, -27.8603, -64.3859),
    "RMA16": ("Villa Reynolds", -35.8888, -67.9675, -31.5478, -62.7835),
    "RMA17": ("Alejandro Roca", -35.5219, -66.2848, -31.1809, -61.1228),
    "RMA18": ("Santa Isabel", -38.394, -69.6088, -34.053, -64.2638),
    "RMA20": ("Las Lajitas", -26.9154, -66.6251, -22.5744, -61.8772),
}
_SINARAME_SIZE = 512
_SINARAME_RADIUS_KM = 240
# Only a radar closer than this: farther out its beam is too high, and
# what's coming has to be inside its 240 km.
_SINARAME_MAX_DISTANCE_KM = 180
_SINARAME_LOOKBACK_SLOTS = 10  # 10-min slots, for its ~25 min delay plus gaps
_SINARAME_FRAMES = 6
# The viewer's legend: one color every 5 dBZ from -15 to 70, linearly
# blended in between. The images use a few more shades inside each band
# than the legend, so a color is matched to the nearest legend shade.
_SINARAME_LEGEND = (
    "3d416b 3b4f78 3d5989 3d6595 3772a6 3189bb 25a1cf 4ce132 3ab027 237219 "
    "c9d333 d69818 c50017 c1005f cb00cd e2f5ee a7ecce 88dfbd"
).split()
# Artifacts seen live, and what keeps them out (plus an opening that drops
# echoes under ~3 km across, for speckle):
# - a radar gone wrong paints concentric rings of extreme values over most
#   of its image (RMA8, RMA13): a frame mostly at 55 dBZ or more is dropped,
# - interference shows up as radial streaks in clear sky (RMA2): no rain
#   where the infrared shows no cloud top colder than -10 °C nearby,
# - no storm-strength echo (45 dBZ+) without tops of -40 °C or colder
#   nearby: that's terrain (the sierras around Córdoba), not a storm,
# - some radars also interleave a sparse scan, with a fraction of the
#   echoes (RMA1), and the sierras' clutter stays put frame after frame:
#   see clean_radar_frames.
_SINARAME_BROKEN_FRACTION = 0.25
_SINARAME_BROKEN_DBZ = 55
_SINARAME_CLOUD_MAX_C = -10
_SINARAME_STORM_CLOUD_MAX_C = -40
_SINARAME_CLOUD_REACH_KM = 8
# Ground clutter around the radar itself, left out of the motion (see
# Field.motion_mask) but not of the rain: a city next to its radar
# (Resistencia, 7 km) still sees what's over it.
_SINARAME_CLUTTER_RADIUS_KM = 20


def sinarame_radars_for(latitude: float, longitude: float) -> list[str]:
    """SINARAME radars within reach of a point, nearest first."""
    found = []
    for radar_id, (_, south, west, north, east) in SINARAME_RADARS.items():
        lat_c, lon_c = (south + north) / 2, (west + east) / 2
        distance = math.hypot(
            (latitude - lat_c) * 110.574, (longitude - lon_c) * 111.320 * math.cos(math.radians(latitude))
        )
        if distance <= _SINARAME_MAX_DISTANCE_KM:
            found.append((distance, radar_id))
    return [radar_id for _, radar_id in sorted(found)]


def _mercator_y(latitude: Any) -> Any:
    import numpy as np

    return np.log(np.tan(np.pi / 4 + np.radians(latitude) / 2))


def infrared_temperature(image: Any) -> Any:
    """GIBS Band13 RGBA -> cloud-top temperature in °C, warm ground included.

    The cold, colored part via decode_infrared; the grays as the warmer
    clouds and ground (-19 °C at gray 197, 0.386 °C per level darker); the
    cyan-to-navy band in between as -25 °C. Missing data reads as warm.
    """
    import numpy as np

    pixels = np.asarray(image.convert("RGBA")).astype(np.int32)
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    temperature = np.full(red.shape, 40.0, dtype=np.float32)
    gray = (np.abs(red - green) < 8) & (np.abs(green - blue) < 8)
    temperature[gray] = -19.1 + (197 - green[gray]) * 0.386
    temperature[(red < 10) & (blue > 90) & ~gray] = -25
    coldness = decode_infrared(image)
    temperature[coldness > 0] = -coldness[coldness > 0]
    temperature[pixels[..., 3] == 0] = 40
    return temperature


def _sinarame_decoder(radar_id: str, infrared_origin: tuple[float, float]) -> Callable[[Any], Any]:
    """Decoder for (radar PNG, infrared mosaic or None) frames of one radar.

    `infrared_origin` is the world pixel (GIBS zoom) of the infrared
    mosaic's top-left corner, to look up each radar pixel's cloud top.
    Each new image also feeds the radar's learned clutter map (see
    ClutterMaps), and the clutter it has learned is taken out.
    """
    import numpy as np

    _, south, west, north, east = SINARAME_RADARS[radar_id]
    size = _SINARAME_SIZE
    colors, values = [], []
    legend = [tuple(int(h[i : i + 2], 16) for i in (0, 2, 4)) for h in _SINARAME_LEGEND]
    for i, (start, end) in enumerate(zip(legend, legend[1:])):
        for step in range(10):
            f = step / 10
            colors.append(tuple(round(a + (b - a) * f) for a, b in zip(start, end)))
            values.append(-15 + 5 * i + 0.5 * step)
    colors.append(legend[-1])
    values.append(70)

    # Each radar pixel's world pixel in the infrared's zoom.
    rows = (np.arange(size) + 0.5) / size
    lats = np.degrees(
        2 * np.arctan(np.exp(_mercator_y(north) - (_mercator_y(north) - _mercator_y(south)) * rows)) - np.pi / 2
    )
    lons = west + (east - west) * (np.arange(size) + 0.5) / size
    world = RADAR_TILE_SIZE * 2**GIBS_MAX_ZOOM_INFRARED
    ir_x = ((lons + 180) / 360 * world - infrared_origin[0]).astype(int)
    ir_y = ((1 - _mercator_y(lats) / np.pi) / 2 * world - infrared_origin[1]).astype(int)
    reach_px = max(1, round(_SINARAME_CLOUD_REACH_KM / km_per_pixel((south + north) / 2, GIBS_MAX_ZOOM_INFRARED)))

    def decode(frame: tuple[Any, Any]) -> Any:
        from PIL import Image, ImageFilter

        radar_image, infrared = frame
        pixels = np.asarray(radar_image.convert("RGBA"))
        dbz = _palette_lookup(pixels[..., :3], colors, values, max_distance=80)
        active = pixels[..., 3] > 0
        dbz[~active] = 0
        if active.sum() >= 500 and (dbz[active] >= _SINARAME_BROKEN_DBZ).mean() > _SINARAME_BROKEN_FRACTION:
            return None
        dbz = np.clip(dbz, 0, None)
        CLUTTER.observe(radar_id, radar_image.info.get(_WHEN_KEY), dbz >= _RADAR_WEAK_DBZ)
        clutter = CLUTTER.mask(radar_id)
        if clutter is not None:
            dbz[clutter] = 0
        # Speckle (ground clutter, a radial of interference): an opening
        # keeps only echoes at least 3 px (~3 km) across.
        echoes = Image.fromarray(((dbz >= _RADAR_WEAK_DBZ) * 255).astype(np.uint8))
        echoes = echoes.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
        dbz[np.asarray(echoes) == 0] = 0
        if infrared is not None:
            # Coldest cloud top within reach of each infrared pixel.
            temperature = infrared_temperature(infrared)
            coldest = np.asarray(
                Image.fromarray(temperature, mode="F").filter(ImageFilter.MinFilter(2 * reach_px + 1))
            )
            h, w = coldest.shape
            nearby = coldest[np.clip(ir_y, 0, h - 1)[:, None], np.clip(ir_x, 0, w - 1)[None, :]]
            dbz[nearby > _SINARAME_CLOUD_MAX_C] = 0
            # Storm-strength echoes without storm tops: terrain (the sierras
            # around Córdoba) far more often than rain.
            dbz[(nearby > _SINARAME_STORM_CLOUD_MAX_C) & (dbz >= RADAR_STORM_DBZ)] = 0
        return dbz

    return decode


async def _fetch_png(session: aiohttp.ClientSession, url: str) -> Any | None:
    """A PNG, or None when it isn't there (404) or can't be fetched."""
    from PIL import Image

    try:
        async with _tile_semaphore(), async_timeout.timeout(10):
            response = await session.get(url)
            if response.status != 200 or "image" not in response.headers.get("Content-Type", ""):
                return None
            data = await response.read()
        return Image.open(io.BytesIO(data))
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Error fetching %s: %s", url, err)
        return None


_WHEN_KEY = "smn_ar_when"
_CLUTTER_DECAY = 0.99  # per image: ~100 images (~17 h) of memory
_CLUTTER_MIN_IMAGES = 36  # ~6 h before it's trusted
_CLUTTER_SHARE = 0.6
# The decayed image count after _CLUTTER_MIN_IMAGES images.
_CLUTTER_MIN_WEIGHT = (1 - _CLUTTER_DECAY**_CLUTTER_MIN_IMAGES) / (1 - _CLUTTER_DECAY)
CLUTTER_FILE = "smn_ar_clutter.npz"


class ClutterMaps:
    """Each SINARAME radar's terrain clutter, learned from its own images.

    Terrain shows up on the same pixels image after image, day after day;
    rain moves on. Every new image (once, by its time) adds to a decaying
    per-pixel count of echoes; a pixel with echoes in over _CLUTTER_SHARE
    of the recent images is clutter (the sierras around Córdoba, seen
    live). Rain parked on one spot for most of a day would be taken for
    clutter too, which is rare enough.

    Shared by the camera and the nowcast (both decode in executor threads,
    hence the lock) and saved to .storage, so a restart doesn't start over.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._counts: dict[str, Any] = {}
        self._images: dict[str, float] = {}
        self._last: dict[str, datetime] = {}
        self._dirty = False

    def observe(self, radar_id: str, when: datetime | None, echoes: Any) -> None:
        import numpy as np

        if when is None:
            return
        with self._lock:
            if radar_id in self._last and when <= self._last[radar_id]:
                return
            self._last[radar_id] = when
            counts = self._counts.get(radar_id)
            if counts is None or counts.shape != echoes.shape:
                counts = np.zeros(echoes.shape, dtype=np.float32)
                self._images[radar_id] = 0.0
            counts *= _CLUTTER_DECAY
            counts += echoes
            self._counts[radar_id] = counts
            self._images[radar_id] = self._images[radar_id] * _CLUTTER_DECAY + 1
            self._dirty = True

    def mask(self, radar_id: str) -> Any | None:
        """Clutter pixels (2D bool), or None while there aren't enough images yet."""
        with self._lock:
            counts = self._counts.get(radar_id)
            images = self._images.get(radar_id, 0.0)
            if counts is None or images < _CLUTTER_MIN_WEIGHT:
                return None
            clutter = counts / images >= _CLUTTER_SHARE
        # Grown a pixel: the edges of a clutter patch come and go.
        from PIL import Image, ImageFilter
        import numpy as np

        grown = Image.fromarray((clutter * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3))
        return np.asarray(grown) > 0

    def load(self, path: str) -> None:
        """Read what was saved (blocking: run it in an executor)."""
        import numpy as np

        try:
            with np.load(path) as data:
                with self._lock:
                    for key in data.files:
                        radar_id, kind = key.rsplit("__", 1)
                        if kind == "counts":
                            self._counts[radar_id] = data[key].astype(np.float32)
                        elif kind == "images":
                            self._images[radar_id] = float(data[key])
                        elif kind == "last":
                            self._last[radar_id] = datetime.fromtimestamp(float(data[key]), timezone.utc)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError) as err:
            _LOGGER.warning("Couldn't read the radar clutter maps, learning them again: %s", err)

    def save(self, path: str) -> None:
        """Write them out if they changed (blocking: run it in an executor)."""
        import numpy as np

        with self._lock:
            if not self._dirty:
                return
            arrays = {}
            for radar_id, counts in self._counts.items():
                arrays[f"{radar_id}__counts"] = counts.astype(np.float16)
                arrays[f"{radar_id}__images"] = np.float64(self._images[radar_id])
                arrays[f"{radar_id}__last"] = np.float64(self._last[radar_id].timestamp())
            self._dirty = False
        tmp = f"{path}.tmp.npz"
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, path)


CLUTTER = ClutterMaps()


async def fetch_sinarame_frames(
    session: aiohttp.ClientSession, radar_id: str, slots: int, now: datetime | None = None
) -> dict[datetime, Any]:
    """A radar's images of the last `slots` 10-min slots that exist, by UTC time, oldest first.

    There's no listing of the frames, so each slot is just tried.
    """
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(_ARG_TZ)
    latest_slot = local.replace(minute=local.minute // 10 * 10, second=0, microsecond=0)
    times = [latest_slot - timedelta(minutes=10 * i) for i in reversed(range(slots))]
    images = await asyncio.gather(
        *(_fetch_png(session, SINARAME_URL.format(radar=radar_id, stamp=t.strftime("%Y%m%d%H%M%S"))) for t in times)
    )
    found = {}
    for t, image in zip(times, images):
        if image is not None:
            image.info[_WHEN_KEY] = t.astimezone(timezone.utc)  # for ClutterMaps.observe
            found[t.astimezone(timezone.utc)] = image
    return found


def sinarame_radars_in_view(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> list[str]:
    """SINARAME radars whose square overlaps an area, farthest from its center first.

    Farthest first so that, drawn in this order, the nearest radar ends up
    on top where they overlap.
    """
    center_lat, center_lon = (min_lat + max_lat) / 2, (min_lon + max_lon) / 2
    found = []
    for radar_id, (_, south, west, north, east) in SINARAME_RADARS.items():
        if south <= max_lat and north >= min_lat and west <= max_lon and east >= min_lon:
            distance = math.hypot((south + north) / 2 - center_lat, (west + east) / 2 - center_lon)
            found.append((distance, radar_id))
    return [radar_id for _, radar_id in sorted(found, reverse=True)]


def sinarame_dbz(radar_id: str, image: Any, infrared: Any, infrared_origin: tuple[float, float]) -> Any | None:
    """One radar image decoded and filtered (see _sinarame_decoder), or None if it's an artifact scan."""
    return _sinarame_decoder(radar_id, infrared_origin)((image, infrared))


def colorize_dbz(dbz: Any, soft: bool = False) -> Any:
    """dBZ array -> RGBA image in RainViewer's Universal Blue, transparent below 15 dBZ.

    So a SINARAME radar reads like the RainViewer one on the same map.
    `soft` fades the edges in from 12 to 20 dBZ instead of a hard cut, for
    a smoothed field.
    """
    import numpy as np
    from PIL import Image

    palette = np.array(
        [[int(h[i : i + 2], 16) for i in (0, 2, 4)] for h in _RADAR_PALETTE], dtype=np.uint8
    )
    index = np.clip(np.round(dbz).astype(int) - _RADAR_PALETTE_START_DBZ, 0, len(palette) - 1)
    rgba = np.zeros((*dbz.shape, 4), dtype=np.uint8)
    rgba[..., :3] = palette[index]
    if soft:
        rgba[..., 3] = (np.clip((dbz - 12) / 8, 0, 1) * 255).astype(np.uint8)
    else:
        rgba[..., 3] = np.where(dbz >= _RADAR_PALETTE_START_DBZ, 255, 0)
    return Image.fromarray(rgba, mode="RGBA")


async def fetch_sinarame_field(
    session: aiohttp.ClientSession,
    radar_id: str,
    latitude: float,
    longitude: float,
    now: datetime | None = None,
) -> Field | None:
    """A SINARAME radar's last frames, with the infrared to filter them.

    There's no listing of the frames: the last _SINARAME_LOOKBACK_SLOTS
    10-min slots are just tried. None when it published nothing lately, or
    when there's no infrared to filter it with.
    """
    name, south, west, north, east = SINARAME_RADARS[radar_id]
    available = list((await fetch_sinarame_frames(session, radar_id, _SINARAME_LOOKBACK_SLOTS, now)).items())
    available = available[-_SINARAME_FRAMES:]
    if not available:
        return None

    # The infrared for each frame (GIBS has those times by now: they're
    # ~25 min old), over the radar's square plus the cloud reach.
    lat_c, lon_c = (south + north) / 2, (west + east) / 2
    ir_url = lambda when: lambda z, x, y: GIBS_TILE_URL_TEMPLATE.format(  # noqa: E731
        layer=GIBS_LAYER_INFRARED,
        time=when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        matrix_set=GIBS_MATRIX_SET_INFRARED,
        z=z,
        x=x,
        y=y,
    )
    ir_radius_km = _SINARAME_RADIUS_KM + 2 * _SINARAME_CLOUD_REACH_KM
    infrared = await asyncio.gather(
        *(
            _fetch_area(session, ir_url(when), GIBS_MAX_ZOOM_INFRARED, lat_c, lon_c, ir_radius_km)
            for when, _ in available
        )
    )
    half = math.ceil(ir_radius_km / km_per_pixel(lat_c, GIBS_MAX_ZOOM_INFRARED))
    cx, cy = _deg2pixel(lat_c, lon_c, GIBS_MAX_ZOOM_INFRARED)
    origin = (round(cx) - half, round(cy) - half)
    # A frame without its own infrared gets the closest one in time.
    complete = [i for i, (_, _, ok) in enumerate(infrared) if ok]
    if not complete:
        # Unfiltered, interference would read as rain: better the next source.
        _LOGGER.debug("No infrared to filter SINARAME's %s with, skipping it", radar_id)
        return None
    frames = [(image, infrared[min(complete, key=lambda j: abs(j - i))][0]) for i, (_, image) in enumerate(available)]

    loc_x = (longitude - west) / (east - west) * _SINARAME_SIZE
    loc_y = float(
        (_mercator_y(north) - _mercator_y(latitude)) / (_mercator_y(north) - _mercator_y(south)) * _SINARAME_SIZE
    )
    import numpy as np

    clutter_px = _SINARAME_CLUTTER_RADIUS_KM / ((north - south) * 110.574 / _SINARAME_SIZE)
    ys, xs = np.ogrid[0:_SINARAME_SIZE, 0:_SINARAME_SIZE]
    near_radar = (xs - _SINARAME_SIZE / 2) ** 2 + (ys - _SINARAME_SIZE / 2) ** 2 <= clutter_px**2
    return Field(
        source=f"{SOURCE_SINARAME} {name}",
        times=[when for when, _ in available],
        frames=frames,
        km_per_px=(north - south) * 110.574 / _SINARAME_SIZE,
        location=(loc_x, loc_y),
        rain=RADAR_RAIN_DBZ,
        storm=RADAR_STORM_DBZ,
        weak=_RADAR_WEAK_DBZ,
        decode=_sinarame_decoder(radar_id, origin),
        motion_mask=near_radar,
        clean=True,
    )
