"""Sensor platform: a human-readable short-term forecast/alert summary.

Synthesizes what the 11+ binary_sensors already expose (active alerts by
event, avisos a muy corto plazo, heat/cold warnings) plus today's forecast
into a single Spanish sentence, so it can be read directly in a dashboard,
a notification, or a TTS announcement without parsing individual sensors.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.weather import ATTR_CONDITION_LIGHTNING_RAINY
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    PERCENTAGE,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alert_levels import (
    active_events_now,
    alert_periods,
    aviso_matches,
    get_alert_radius_km,
    ranked_avisos,
)
from .const import (
    ALERT_EVENT_MAP,
    ALERT_EVENT_NEARBY_KEYWORDS,
    ALERT_LEVEL_MAP,
    DOMAIN,
    DAILY_RAIN_CALIBRATION,
    HOURLY_RAIN_ODDS,
    NEXT_RAIN_CURVE_BLEND,
    NEXT_RAIN_HOURLY_MIN_PRECIPITATION,
    NEXT_RAIN_HOURLY_PROBABILITY,
    NEXT_RAIN_CURVE_CALIBRATION,
    NEXT_RAIN_PROBABILITY_THRESHOLD,
    NEXT_RAIN_RADAR_ODDS,
    NEXT_RAIN_REFINE_MIN_PRECIPITATION,
    NEXT_RAIN_REFINE_PROBABILITY,
    NEXT_RAIN_SATELLITE_ODDS,
    NEXT_RAIN_STORM_ODDS,
    OPEN_METEO_MODELS,
    STORM_ODDS_CALIBRATION,
    STORM_ODDS_FALLBACK,
    wind_cardinal,
)
from .coordinator import ArgentinaSMNDataUpdateCoordinator
from .nowcast import SOURCE_RADAR, SOURCE_SATELLITE, Nowcast, aviso_arrival, steering_motion
from .weather import format_condition

_LOGGER = logging.getLogger(__name__)

MAX_STATE_LENGTH = 255

# Argentina doesn't observe DST, so a fixed UTC-3 offset is always correct
# for SMN's forecast timestamps regardless of where the HA host itself is
# configured — same reasoning satellite.py uses for its frame captions.
_ARG_TZ = timezone(timedelta(hours=-3))


def _parse_period_datetime(date_str: str | None, time_str: str | None) -> datetime | None:
    """Parse an hourly_forecast period's separate date/time strings as Argentina local time."""
    if not date_str or not time_str:
        return None
    try:
        naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=_ARG_TZ)


_RAIN_EVENT_ID = 37  # ALERT_EVENT_MAP[37] == "lluvia"
_STORM_EVENT_ID = 41  # ALERT_EVENT_MAP[41] == "tormenta"
_STORM_KEYWORDS = ALERT_EVENT_NEARBY_KEYWORDS["tormenta"]
_RAIN_OR_STORM_KEYWORDS = (
    ALERT_EVENT_NEARBY_KEYWORDS["tormenta"][0] + ALERT_EVENT_NEARBY_KEYWORDS["lluvia"][0],
    (),
)
# Source strength, used to break ties at the same time.
_SOURCE_AVISO = (4, "aviso cercano")
_SOURCE_ALERT = (3, "alerta de zona")
_SOURCE_HOURLY = (2, "pronóstico horario")
_SOURCE_FORECAST = (1, "pronóstico")
_NOWCAST_STRENGTH = 5  # the nowcast's own source name (radar / satélite) goes with it
_PERIOD = timedelta(hours=6)
_RADAR_ODDS_WINDOW = timedelta(hours=2)
# The curve starts up to this long before the first rainy hour.
_CURVE_LEAD = timedelta(hours=2)
_HOUR = timedelta(hours=1)
_LOGIT_CLIP = 0.002
_DIRECTIONS_ES = {
    "N": "norte", "NNE": "nornoreste", "NE": "noreste", "ENE": "estenoreste",
    "E": "este", "ESE": "estesureste", "SE": "sureste", "SSE": "sursureste",
    "S": "sur", "SSW": "sursuroeste", "SW": "suroeste", "WSW": "oestesuroeste",
    "W": "oeste", "WNW": "oestenoroeste", "NW": "noroeste", "NNW": "nornoroeste",
}
_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_LEVEL_NAMES_ES = {"amarillo": "amarilla", "naranja": "naranja", "rojo": "roja"}


def _parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _period_start(when: datetime) -> datetime:
    """Start of the SMN period (00/06/12/18 Argentina time) `when` falls in."""
    local = when.astimezone(_ARG_TZ)
    return local.replace(hour=local.hour // 6 * 6, minute=0, second=0, microsecond=0)


def _hourly_is_rain(hour: dict[str, Any], storm: int | None = None) -> str | None:
    """Whether an Open-Meteo hour counts as "tormenta", "lluvia" or neither (None).

    `storm` is the chance of a storm in the 3 hours from it (storm_odds);
    the hour itself must bring some rain too (NEXT_RAIN_REFINE_*), so the
    storm starts when the rain does and not up to 2 hours before.
    """
    probability = hour.get("probability") or 0
    precipitation = hour.get("precipitation") or 0
    if (
        storm is not None
        and storm >= NEXT_RAIN_STORM_ODDS
        and probability >= NEXT_RAIN_REFINE_PROBABILITY
        and precipitation >= NEXT_RAIN_REFINE_MIN_PRECIPITATION
    ):
        return "tormenta"
    if probability >= NEXT_RAIN_HOURLY_PROBABILITY and precipitation >= NEXT_RAIN_HOURLY_MIN_PRECIPITATION:
        return "lluvia"
    return None


def _has_models(hours: list[dict[str, Any]]) -> bool:
    """Whether every hour has the AIFS and UKMO rain (OPEN_METEO_MODELS)."""
    return all(hour.get(key) is not None for hour in hours for key in OPEN_METEO_MODELS)


def _sigmoid_pct(logit: float) -> int:
    return round(100 / (1 + math.exp(-logit)))


def storm_odds(open_meteo_hourly: list[dict[str, Any]], start: datetime) -> int | None:
    """% chance of a thunderstorm in the 3 hours from `start` (an Open-Meteo hour).

    See STORM_ODDS_CALIBRATION; STORM_ODDS_FALLBACK when the AIFS/UKMO rain
    is missing. None when Open-Meteo doesn't have all 3 hours.
    """
    by_hour = {hour["when"]: hour for hour in open_meteo_hourly}
    hours = [by_hour.get(start + index * _HOUR) for index in range(3)]
    if any(hour is None or hour.get("probability") is None for hour in hours):
        return None
    probability = max(hour["probability"] for hour in hours) / 100
    cape = math.log1p(max(hour.get("cape") or 0 for hour in hours))
    angle = 2 * math.pi * start.astimezone(timezone.utc).hour / 24
    clock = (math.sin(angle), math.cos(angle))  # hour of day
    if _has_models(hours):
        c0, c1, c2, c3, c4, c5, c6 = STORM_ODDS_CALIBRATION
        aifs = math.log1p(sum(hour["precipitation_aifs"] for hour in hours))
        ukmo = math.log1p(sum(hour["precipitation_ukmo"] for hour in hours))
        return _sigmoid_pct(c0 + c1 * probability + c2 * aifs + c3 * ukmo + c4 * cape + c5 * clock[0] + c6 * clock[1])
    c0, c1, c4, c5, c6 = STORM_ODDS_FALLBACK
    return _sigmoid_pct(c0 + c1 * probability + c4 * cape + c5 * clock[0] + c6 * clock[1])


def daily_rain_odds(open_meteo_models: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Chance of rain tomorrow and the next two days (Argentine days), from AIFS.

    See DAILY_RAIN_CALIBRATION. Each: `fecha`, `probabilidad` (%) and
    `mm` (AIFS's rain over the day); a day AIFS doesn't fully cover is left out.
    """
    totals: dict[Any, list[float]] = {}
    for hour in open_meteo_models:
        if hour.get("precipitation_aifs") is None:
            continue
        # Open-Meteo's value at HH is the rain in the hour before it.
        day = (hour["when"] - _HOUR).astimezone(_ARG_TZ).date()
        totals.setdefault(day, []).append(hour["precipitation_aifs"])
    today = now.astimezone(_ARG_TZ).date()
    days = []
    for ahead, (a, b) in enumerate(DAILY_RAIN_CALIBRATION, start=1):
        day = today + timedelta(days=ahead)
        values = totals.get(day, [])
        if len(values) < 24:
            continue
        mm = sum(values)
        days.append({"fecha": day.isoformat(), "probabilidad": _sigmoid_pct(a + b * math.log1p(mm)), "mm": round(mm, 1)})
    return days


def _odds(table: tuple[tuple[int, int], ...], probability: float | None) -> int | None:
    """The measured % for a value, from a table of (minimum, odds)."""
    if probability is None:
        return None
    return next(odds for minimum, odds in table if probability >= minimum)


def _logit(value: float) -> float:
    value = min(max(value, _LOGIT_CLIP), 1 - _LOGIT_CLIP)
    return math.log(value / (1 - value))


def rain_curve(open_meteo_hourly: list[dict[str, Any]], start: datetime) -> list[tuple[datetime, int]]:
    """(end of the window, % chance of rain from `start` to it), one per hour up to 6 h.

    See NEXT_RAIN_CURVE_BLEND, or NEXT_RAIN_CURVE_CALIBRATION once an hour
    lacks the AIFS/UKMO rain. Starts at the first hour Open-Meteo has from
    `start` on and stops where its hours run out. The windows are nested,
    so a longer one is never less likely.
    """
    by_hour = {hour["when"]: hour for hour in open_meteo_hourly}
    start = min((when for when in by_hour if when >= start), default=start)
    curve: list[tuple[datetime, int]] = []
    dry = 1.0
    aifs = ukmo = 0.0
    blend = True
    highest = 0
    for index, ((a, b), (ba, bb, bc, bd)) in enumerate(zip(NEXT_RAIN_CURVE_CALIBRATION, NEXT_RAIN_CURVE_BLEND)):
        hour = by_hour.get(start + index * _HOUR)
        if hour is None or hour.get("probability") is None:
            break
        dry *= 1 - _odds(HOURLY_RAIN_ODDS, hour["probability"]) / 100
        blend = blend and _has_models([hour])
        if blend:
            aifs += hour["precipitation_aifs"]
            ukmo += hour["precipitation_ukmo"]
            logit = ba + bb * _logit(1 - dry) + bc * math.log1p(aifs) + bd * math.log1p(ukmo)
        else:
            logit = a + b * _logit(1 - dry)
        highest = max(highest, _sigmoid_pct(logit))
        curve.append((start + (index + 1) * _HOUR, highest))
    return curve


def _nowcast_odds(nowcast: Nowcast) -> int | None:
    """The measured % for a nowcast: the radar by its echo, the infrared only right overhead."""
    if nowcast.source.startswith(SOURCE_RADAR):
        return _odds(NEXT_RAIN_RADAR_ODDS, nowcast.intensity)
    if nowcast.source == SOURCE_SATELLITE and not nowcast.distance_km:
        return NEXT_RAIN_SATELLITE_ODDS
    return None


def find_next_rain_or_storm(
    hourly_forecast: list[dict[str, Any]],
    alerts: dict[str, Any] | None,
    avisos: list[dict[str, Any]] | None,
    latitude: float,
    longitude: float,
    radius_km: float,
    now: datetime | None = None,
    open_meteo_hourly: list[dict[str, Any]] | None = None,
    nowcast: Nowcast | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(next rain or storm, next storm) combining every source, most precise first.

    Candidates, each with `when`, `tipo` ("lluvia"/"tormenta") and `source`:
    - the nowcast (nowcast.py): the radar or infrared echoes extrapolated
      along their motion, to the minute, up to 2h ahead,
    - a rain/storm aviso a muy corto plazo within the radius: when its area,
      moving like the storms do, reaches the location (right away if it's
      already inside; one that moves past is dropped). Without a motion,
      when it was issued, as if it were already here,
    - a rain/storm alert for the location's zone, per period,
    - Open-Meteo's hourly forecast: an hour with rain likely, or a storm,
    - SMN's forecast periods with rain likely (the middle of its
      probability range at NEXT_RAIN_PROBABILITY_THRESHOLD or more) or a
      storm condition, even with a low probability.
    SMN's periods and zone alerts are 6 hours long and cover a whole zone:
    one only counts with an hour in it where Open-Meteo also gives some
    rain, and that hour is when it starts (without Open-Meteo, the start of
    the period). Where there's radar, the forecasts still count within its
    horizon: drizzle and light rain are under its threshold (RADAR_RAIN_DBZ).
    The earliest wins; anything already
    under way counts as "now", and ties go to the stronger source.

    `odds` is the chance of rain before `odds_until`, None where it couldn't
    be measured: the avisos, the infrared's projected arrivals. For the
    forecast sources it's the end of `curve` (see rain_curve), which starts
    at most _CURVE_LEAD before `when`, and `hour_odds` is for the very hour
    of `when` (HOURLY_RAIN_ODDS).
    """
    now = now or datetime.now(_ARG_TZ)
    open_meteo_hourly = open_meteo_hourly or []
    if nowcast is not None and not nowcast.is_current(now):
        nowcast = None
    motion = (nowcast.motion if nowcast else None) or steering_motion(open_meteo_hourly, now)
    candidates: list[dict[str, Any]] = []

    def refine(start: datetime) -> dict[str, Any] | None:
        """`when` from the first not-yet-past hour of a 6-hour period Open-Meteo gives some rain in.

        None when it gives none (see NEXT_RAIN_REFINE_PROBABILITY); the
        period's start when there's no Open-Meteo to tell.
        """
        if not open_meteo_hourly:
            return {"when": start}
        for hour in open_meteo_hourly:
            if (
                start <= hour["when"] < start + _PERIOD
                and hour["when"] + timedelta(hours=1) > now
                and (hour.get("probability") or 0) >= NEXT_RAIN_REFINE_PROBABILITY
                and (hour.get("precipitation") or 0) >= NEXT_RAIN_REFINE_MIN_PRECIPITATION
            ):
                return {
                    "when": hour["when"],
                    "hour_odds": _odds(HOURLY_RAIN_ODDS, hour.get("probability")),
                    "hourly_probability": hour.get("probability"),
                }
        return None

    if nowcast is not None and nowcast.arrival is not None:
        odds = _nowcast_odds(nowcast)
        candidates.append(
            {
                "when": nowcast.arrival,
                "tipo": nowcast.tipo,
                "source": (_NOWCAST_STRENGTH, nowcast.source),
                "until": nowcast.until,
                "distance_km": nowcast.distance_km,
                "direction": wind_cardinal(nowcast.from_deg),
                "intensity": nowcast.intensity,
                "odds": odds,
                # The radar's odds are for rain within 2 h; the infrared's window wasn't measured.
                "odds_until": now + _RADAR_ODDS_WINDOW if odds is not None and nowcast.source.startswith(SOURCE_RADAR) else None,
            }
        )

    for distance, bearing, aviso in ranked_avisos(latitude, longitude, avisos, _RAIN_OR_STORM_KEYWORDS):
        if distance > radius_km:
            continue
        when = _parse_iso(aviso.get("date")) or now
        if distance > 0 and motion is not None:
            when = aviso_arrival(latitude, longitude, aviso, when, motion)
            if when is None:
                continue
        candidates.append(
            {
                "when": when,
                "tipo": "tormenta" if aviso_matches(aviso, _STORM_KEYWORDS) else "lluvia",
                "source": _SOURCE_AVISO,
                "distance_km": round(distance, 1),
                "direction": None if distance == 0 else wind_cardinal(bearing),
                "title": (aviso.get("title") or "").strip(),
                "end_date": aviso.get("end_date"),
            }
        )

    for event_id, tipo in ((_STORM_EVENT_ID, "tormenta"), (_RAIN_EVENT_ID, "lluvia")):
        for start, level in alert_periods(alerts, event_id, now):
            if (refined := refine(start)) is None:
                continue
            candidates.append(
                {
                    **refined,
                    "tipo": tipo,
                    "source": _SOURCE_ALERT,
                    "level": level,
                    "level_name": ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])["name"],
                }
            )

    # The first hour with rain or a storm, and the first one with a storm.
    upcoming = []
    for hour in open_meteo_hourly:
        if hour["when"] + timedelta(hours=1) > now:
            storm = storm_odds(open_meteo_hourly, hour["when"])
            if tipo := _hourly_is_rain(hour, storm):
                upcoming.append((hour, tipo, storm))
    for hour, tipo, storm in upcoming[:1] + [u for u in upcoming if u[1] == "tormenta"][:1]:
        candidates.append(
            {
                "when": hour["when"],
                "tipo": tipo,
                "storm_odds": storm,
                "source": _SOURCE_HOURLY,
                "hourly_probability": hour.get("probability"),
                "hour_odds": _odds(HOURLY_RAIN_ODDS, hour.get("probability")),
            }
        )

    forecast_by_period: dict[datetime, dict[str, Any]] = {}
    for period in hourly_forecast:
        when = _parse_period_datetime(period.get("date"), period.get("time"))
        if when is None:
            continue
        rain_prob_range = period.get("rain_prob_range")
        probability = max(rain_prob_range) if rain_prob_range else None
        likelihood = sum(rain_prob_range) / len(rain_prob_range) if rain_prob_range else None
        condition = format_condition(period.get("weather"))
        forecast_by_period[when] = {"smn_probability": probability, "condition": condition}
        if when + _PERIOD <= now:
            continue
        storm = condition == ATTR_CONDITION_LIGHTNING_RAINY
        if not storm and (likelihood is None or likelihood < NEXT_RAIN_PROBABILITY_THRESHOLD):
            continue
        if (refined := refine(when)) is None:
            continue
        candidates.append({**refined, "tipo": "tormenta" if storm else "lluvia", "source": _SOURCE_FORECAST})

    def sort_key(candidate: dict[str, Any]) -> tuple[datetime, int, int]:
        return (
            max(candidate["when"], now),
            -candidate["source"][0],
            0 if candidate["tipo"] == "tormenta" else 1,
        )

    candidates.sort(key=sort_key)
    current_hour = now.astimezone(_ARG_TZ).replace(minute=0, second=0, microsecond=0)
    for candidate in candidates:
        if candidate["source"][0] < _SOURCE_AVISO[0]:
            when_hour = candidate["when"].astimezone(_ARG_TZ).replace(minute=0, second=0, microsecond=0)
            curve = rain_curve(open_meteo_hourly, max(current_hour, when_hour - _CURVE_LEAD))
            candidate["curve"] = curve
            if curve:
                candidate["odds_until"], candidate["odds"] = curve[-1]
        # Rain probability/condition SMN's forecast gives for the same period.
        candidate.update(forecast_by_period.get(_period_start(candidate["when"]), {}))
        if motion is not None:
            candidate["motion"] = motion
    next_any = candidates[0] if candidates else None
    next_storm = next((c for c in candidates if c["tipo"] == "tormenta"), None)
    return next_any, next_storm


def _day_and_time(when: datetime, now: datetime) -> str:
    """"las 14:00", "mañana a las 14:00" or "el lunes a las 14:00"."""
    local = when.astimezone(_ARG_TZ)
    days = (local.date() - now.astimezone(_ARG_TZ).date()).days
    clock = f"las {local:%H:%M}"
    if days <= 0:
        return clock
    return f"mañana a {clock}" if days == 1 else f"el {_WEEKDAYS_ES[local.weekday()]} a {clock}"


def next_rain_message(
    next_any: dict[str, Any] | None, next_storm: dict[str, Any] | None, now: datetime
) -> dict[str, str]:
    """`titulo`, `detalle`, `icono` and `color` ready to show, so a card only displays them.

    Uses clock times, not "in 40 min": the text only changes with a
    coordinator update, and a countdown would go stale in between.
    """
    if next_any is None:
        return {
            "titulo": "Sin lluvia a la vista",
            "detalle": "Nada en el radar ni en los pronósticos",
            "icono": "mdi:weather-partly-cloudy",
            "color": "green",
        }
    tipo = next_any["tipo"]
    strength = next_any["source"][0]
    observed = strength >= _SOURCE_AVISO[0]
    under_way = next_any["when"] <= now
    odds = next_any.get("odds")

    if under_way and strength == _SOURCE_AVISO[0]:
        titulo = f"Aviso de {tipo} vigente"
    elif under_way and observed:
        titulo = f"{tipo.capitalize()} en curso"
    elif under_way:
        titulo = f"{tipo.capitalize()} posible ahora"
    elif observed:
        titulo = f"{tipo.capitalize()} cerca de {_day_and_time(next_any['when'], now)}"
    else:
        titulo = f"{tipo.capitalize()} posible desde {_day_and_time(next_any['when'], now)}"

    parts = []
    if next_any.get("curve"):
        first_hour_end = next_any["when"] + _HOUR
        first = next(((end, pct) for end, pct in next_any["curve"] if end >= first_hour_end), None)
        if first and first[1] != odds:
            parts.append(f"{first[1]}% antes de las {first[0].astimezone(_ARG_TZ):%H:%M}")
    if odds is not None:
        until = next_any.get("odds_until")
        parts.append(f"{odds}% antes de las {until.astimezone(_ARG_TZ):%H:%M}" if until else f"{odds}%")
    if next_any.get("level_name"):
        name = next_any["level_name"]
        parts.append(f"alerta {_LEVEL_NAMES_ES.get(name, name)}")
    if next_any.get("distance_km"):
        place = f"a {next_any['distance_km']:.0f} km"
        if next_any.get("direction") in _DIRECTIONS_ES:
            place += f" al {_DIRECTIONS_ES[next_any['direction']]}"
        parts.append(place)
    if not next_any.get("level_name"):
        parts.append(f"según {next_any['source'][1]}")
    if next_storm is not None and next_storm is not next_any:
        parts.append(f"tormenta posible desde {_day_and_time(next_storm['when'], now)}")

    if under_way and observed:
        icono = "mdi:weather-lightning-rainy" if tipo == "tormenta" else "mdi:weather-pouring"
        color = "red" if tipo == "tormenta" else "blue"
    else:
        icono = "mdi:weather-cloudy-clock"
        color = "orange" if observed or (odds or 0) >= 50 else "blue-grey"
    return {"titulo": titulo, "detalle": " · ".join(parts), "icono": icono, "color": color}


def _format_time(iso_str: str | None) -> str | None:
    """Extract HH:MM from an ISO datetime string, best-effort."""
    if not iso_str or "T" not in iso_str:
        return None
    try:
        return iso_str.split("T", 1)[1][:5]
    except (IndexError, ValueError):
        return None


def _active_event_alerts(alerts: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the zone's alert events in the current period (level > 1), most severe first."""
    active = []
    for event in active_events_now(alerts if isinstance(alerts, dict) else None):
        event_id = event["event_id"]
        level = event["level"]
        level_info = ALERT_LEVEL_MAP.get(level, {"name": "alerta"})
        active.append(
            {
                "event": ALERT_EVENT_MAP.get(event_id, f"evento_{event_id}"),
                "level": level,
                "level_name": level_info["name"],
            }
        )

    active.sort(key=lambda a: a["level"], reverse=True)
    return active


def build_summary(
    shortterm_alerts: list[dict[str, Any]],
    alerts: dict[str, Any],
    heat_warnings: dict[str, Any],
    cold_warnings: dict[str, Any],
    daily_forecast: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Build (short state text, extra attributes) summarizing the short term."""
    attrs: dict[str, Any] = {}

    # 1. Avisos a muy corto plazo (1-2h validity) take priority: most urgent.
    if shortterm_alerts:
        first = shortterm_alerts[0]
        title = (first.get("title") or "").strip().rstrip(".").capitalize()
        end_time = _format_time(first.get("end_date"))
        zones = first.get("zones") or []
        zone_text = zones[0] if zones else ""

        state = f"⚠️ {title}"
        if end_time:
            state += f" (vigente hasta las {end_time})"
        if zone_text:
            state = f"{state}. {zone_text}"

        attrs["avisos_corto_plazo"] = [
            {
                "titulo": a.get("title"),
                "emitido": a.get("date"),
                "vigente_hasta": a.get("end_date"),
                "zonas": a.get("zones"),
                "region": a.get("region"),
                "instrucciones": a.get("instructions"),
            }
            for a in shortterm_alerts
        ]
        # Full protective-measures text for the most urgent aviso, straight
        # from the SMN API (warning/shortterm's "instructions" field) — no
        # need to scrape the website's HTML for this, it's already in the
        # JSON we fetch.
        if first.get("instructions"):
            attrs["instrucciones"] = first["instructions"]
        return state[:MAX_STATE_LENGTH], attrs

    # 2. Alertas por evento (tormenta, lluvia, viento, etc.) de la zona,
    # en la franja actual del día.
    active_events = _active_event_alerts(alerts)
    if active_events:
        parts = [f"{a['event'].replace('_', ' ')} ({a['level_name']})" for a in active_events]
        state = "Alerta de " + ", ".join(parts) + " para tu zona"
        attrs["alertas_activas"] = active_events
        return state[:MAX_STATE_LENGTH], attrs

    # 3. Olas de calor/frío.
    heat_level = heat_warnings.get("level", 1) if heat_warnings else 1
    cold_level = cold_warnings.get("level", 1) if cold_warnings else 1
    if heat_level > 1:
        return f"Alerta por ola de calor (nivel {heat_level})"[:MAX_STATE_LENGTH], attrs
    if cold_level > 1:
        return f"Alerta por ola de frío (nivel {cold_level})"[:MAX_STATE_LENGTH], attrs

    # 4. Sin alertas: resumen del pronóstico de hoy.
    if daily_forecast:
        today = daily_forecast[0]
        condition = format_condition(today.get("weather"))
        temp_max = today.get("temp_max")
        temp_min = today.get("temp_min")
        state = "Sin alertas vigentes."
        if temp_max is not None and temp_min is not None:
            state += f" Hoy: {condition}, máxima {temp_max:.0f}°C / mínima {temp_min:.0f}°C"
        return state[:MAX_STATE_LENGTH], attrs

    return "Sin alertas vigentes", attrs


def build_nationwide_summary(
    nationwide_alerts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Build (short state text, extra attributes) for the nationwide avisos.

    Groups the current avisos a muy corto plazo (no location filter) by
    province — the same view as smn.gob.ar's "Resumen por provincia".
    """
    if not nationwide_alerts:
        return "Sin avisos vigentes en el país", {"por_provincia": {}}

    by_province: dict[str, list[dict[str, Any]]] = {}
    for alert in nationwide_alerts:
        provinces = alert.get("provinces") or []
        province_names = [p.get("name") for p in provinces if p.get("name")]
        if not province_names:
            # Fall back to parsing "PROVINCIA: departamentos..." from zones.
            province_names = [
                z.split(":", 1)[0].strip().title() for z in (alert.get("zones") or [])
            ]

        entry = {
            "titulo": alert.get("title"),
            "vigente_hasta": alert.get("end_date"),
            "zonas": alert.get("zones"),
        }
        for name in province_names or ["(sin provincia)"]:
            by_province.setdefault(name, []).append(entry)

    province_list = sorted(by_province.keys())
    state = f"{len(nationwide_alerts)} avisos vigentes en {', '.join(province_list)}"

    return state[:MAX_STATE_LENGTH], {
        "total_avisos": len(nationwide_alerts),
        "por_provincia": by_province,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN summary sensors."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    async_add_entities(
        [
            SMNShortTermSummarySensor(coordinator, config_entry),
            SMNNationwideAvisosSensor(coordinator, config_entry),
            SMNTemperatureSensor(coordinator, config_entry),
            SMNFeelsLikeSensor(coordinator, config_entry),
            SMNHumiditySensor(coordinator, config_entry),
            SMNWindSpeedSensor(coordinator, config_entry),
            SMNWindBearingSensor(coordinator, config_entry),
            SMNTodayForecastSensor(coordinator, config_entry),
            SMNTomorrowForecastSensor(coordinator, config_entry),
            SMNNextRainSensor(coordinator, config_entry),
            SMNStormOddsSensor(coordinator, config_entry),
            SMNRainTomorrowSensor(coordinator, config_entry),
        ]
    )


class _SMNSensorBase(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity):
    """Shared device_info boilerplate for the standalone current/forecast sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}{unique_id_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )


class SMNTemperatureSensor(_SMNSensorBase):
    """Current temperature, as its own entity (separate from the weather entity)."""

    _attr_translation_key = "current_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("temperature")


class SMNFeelsLikeSensor(_SMNSensorBase):
    """Current apparent ("feels like") temperature.

    SMN's own API returns `null` for this under most conditions (not just
    extreme heat/cold), so when that happens the coordinator fills it in
    with an estimate from temperature/humidity/wind (see
    coordinator._estimate_feels_like) instead of leaving the sensor
    permanently "Unknown" — `feels_like_is_estimate` says which case this
    reading is, so it isn't confused with SMN's own number.
    """

    _attr_translation_key = "feels_like_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_feels_like_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("feels_like")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "is_estimate": self.coordinator.data.current_weather_data.get(
                "feels_like_is_estimate", False
            )
        }


class SMNHumiditySensor(_SMNSensorBase):
    """Current relative humidity."""

    _attr_translation_key = "current_humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_humidity")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("humidity")


class SMNWindSpeedSensor(_SMNSensorBase):
    """Current wind speed."""

    _attr_translation_key = "current_wind_speed"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_wind_speed")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("wind_speed")


class SMNWindBearingSensor(_SMNSensorBase):
    """Current wind direction, as a 16-point compass label (e.g. "NE").

    The degree value is kept as an attribute rather than the state, since a
    compass label reads better on a dashboard/automation than a raw number.
    """

    _attr_translation_key = "current_wind_bearing"
    _attr_icon = "mdi:compass-outline"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_wind_bearing")

    @property
    def native_value(self) -> str | None:
        return wind_cardinal(self.coordinator.data.current_weather_data.get("wind_deg"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        degrees = self.coordinator.data.current_weather_data.get("wind_deg")
        return {"degrees": degrees} if degrees is not None else {}


class _SMNDailyForecastSensor(_SMNSensorBase):
    """Shared logic for a single day's forecast (today or tomorrow) as a sensor.

    State is the day's condition (e.g. "sunny"); max/min temperature and the
    rest of the day's data are exposed as attributes, since a sensor only
    has one state value but dashboards/automations may want the numbers too.
    """

    _day_index: int

    @property
    def _day(self) -> dict[str, Any] | None:
        forecast = self.coordinator.data.daily_forecast
        if not forecast or len(forecast) <= self._day_index:
            return None
        return forecast[self._day_index]

    @property
    def native_value(self) -> str | None:
        day = self._day
        if day is None:
            return None
        return format_condition(day.get("weather"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        day = self._day
        if day is None:
            return {}
        return {
            "temp_max": day.get("temp_max"),
            "temp_min": day.get("temp_min"),
            "date": day.get("date"),
        }


class SMNTodayForecastSensor(_SMNDailyForecastSensor):
    """Today's forecast condition, with max/min temperature as attributes."""

    _attr_translation_key = "today_forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _day_index = 0

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_today_forecast")


class SMNTomorrowForecastSensor(_SMNDailyForecastSensor):
    """Tomorrow's forecast condition, with max/min temperature as attributes."""

    _attr_translation_key = "tomorrow_forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _day_index = 1

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_tomorrow_forecast")


class SMNNextRainSensor(_SMNSensorBase):
    """Próxima lluvia o tormenta: when the next rain or storm is expected.

    Combines the nowcast, a nearby aviso, the zone's rain/storm alerts by
    period and the forecasts (see find_next_rain_or_storm). State is a
    timestamp (device_class TIMESTAMP), so a dashboard shows it as "en 3
    horas" and it's directly usable in automation triggers/conditions.
    `tipo` tells rain and storm apart; `proxima_tormenta` keeps the next
    storm even when rain comes first. `None` means nothing is forecast in
    any source's horizon.
    """

    _attr_translation_key = "next_rain"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:weather-pouring"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_next_rain")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self.coordinator.nowcast is not None:
            self.async_on_remove(
                self.coordinator.nowcast.async_add_listener(self._handle_coordinator_update)
            )

    @property
    def _radius_km(self) -> float:
        return get_alert_radius_km(self._config_entry)

    def _find(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        data = self.coordinator.data
        nowcast = self.coordinator.nowcast
        return find_next_rain_or_storm(
            data.hourly_forecast,
            data.alerts,
            data.nationwide_shortterm_alerts,
            self._config_entry.data[CONF_LATITUDE],
            self._config_entry.data[CONF_LONGITUDE],
            self._radius_km,
            open_meteo_hourly=data.open_meteo_hourly,
            nowcast=nowcast.data if nowcast is not None else None,
        )

    @property
    def native_value(self) -> datetime | None:
        next_any, _ = self._find()
        return next_any["when"] if next_any else None

    @property
    def icon(self) -> str:
        next_any, _ = self._find()
        return "mdi:weather-lightning-rainy" if next_any and next_any["tipo"] == "tormenta" else "mdi:weather-pouring"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        next_any, next_storm = self._find()
        now = datetime.now(_ARG_TZ)
        attrs: dict[str, Any] = {
            "criterio": (
                "Lo más próximo entre: la lluvia o tormenta que el radar (SINARAME o "
                "RainViewer) o el satélite infrarrojo ven acercarse (hasta 2 horas), un aviso a corto plazo de lluvia o "
                f"tormenta a menos de {self._radius_km:g} km que se mueve hacia tu ubicación, "
                "el pronóstico horario de Open-Meteo, y una alerta de lluvia o tormenta para tu zona "
                f"o el pronóstico del SMN (lluvia con {NEXT_RAIN_PROBABILITY_THRESHOLD}% o más de "
                "probabilidad, o tormenta) cuando Open-Meteo también da lluvia en esa franja. "
                "Del pronóstico horario, una hora cuenta como tormenta con "
                f"{NEXT_RAIN_STORM_ODDS}% o más de probabilidad de tormenta en las 3 horas siguientes "
                "(ver el sensor Probabilidad de tormenta). "
                "Es una estimación, no una certeza: `probabilidad` dice cuántas veces llovió de verdad "
                "antes de `probabilidad_hasta` en situaciones así (medido contra observaciones de "
                "aeropuertos), y `probabilidad_hora` lo mismo sólo para la hora de inicio"
            ),
            "rain_expected": next_any is not None,
            "proxima_tormenta": next_storm["when"].isoformat() if next_storm else None,
            **next_rain_message(next_any, next_storm, now),
        }
        if next_any is None:
            return attrs
        attrs.update(
            {
                "tipo": next_any["tipo"],
                "fuente": next_any["source"][1],
                # Already under way (a current aviso or period): the state is
                # when it started, so it reads "hace X" on a dashboard.
                "en_curso": next_any["when"] <= now,
                "minutos": max(0, round((next_any["when"] - now).total_seconds() / 60)),
                "probabilidad": next_any.get("odds"),
                "probabilidad_hasta": next_any["odds_until"].isoformat() if next_any.get("odds_until") else None,
                "probabilidad_hora": next_any.get("hour_odds"),
                "probabilidad_smn": next_any.get("smn_probability"),
                "curva": [
                    {"hasta": end.isoformat(), "probabilidad": pct} for end, pct in next_any.get("curve") or []
                ],
                "condition": next_any.get("condition"),
            }
        )
        for key in (
            "level",
            "level_name",
            "distance_km",
            "direction",
            "title",
            "end_date",
            "intensity",
            "hourly_probability",
        ):
            if next_any.get(key) is not None:
                attrs[key] = next_any[key]
        if next_any.get("storm_odds") is not None:
            attrs["probabilidad_tormenta"] = next_any["storm_odds"]
        if next_any.get("until") is not None:
            attrs["hasta"] = next_any["until"].isoformat()
        motion = next_any.get("motion")
        if motion is not None:
            attrs["velocidad_kmh"] = motion.speed_kmh
            attrs["se_mueve_hacia"] = wind_cardinal(motion.toward_deg)
            attrs["movimiento_segun"] = motion.source
        return attrs


_MODELS_SOURCE = "Open-Meteo: best_match, ECMWF AIFS y UK Met Office"


class SMNStormOddsSensor(_SMNSensorBase):
    """Probabilidad de tormenta: % chance of a thunderstorm in the next 3 hours.

    See storm_odds. `proximas_horas` has the same for each 3 hours of the
    next day, and `maxima` / `maxima_desde` the highest of them.
    """

    _attr_translation_key = "storm_odds"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:weather-lightning"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_storm_odds")

    def _windows(self) -> list[tuple[datetime, int]]:
        hourly = self.coordinator.data.open_meteo_hourly
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        windows = []
        for index in range(8):
            when = start + index * 3 * _HOUR
            odds = storm_odds(hourly, when)
            if odds is not None:
                windows.append((when, odds))
        return windows

    @property
    def native_value(self) -> int | None:
        windows = self._windows()
        return windows[0][1] if windows and windows[0][0] <= datetime.now(timezone.utc) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        windows = self._windows()
        hourly = self.coordinator.data.open_meteo_hourly
        current = [hour for hour in hourly if windows and hour["when"] == windows[0][0]]
        attrs: dict[str, Any] = {
            "criterio": (
                "Cuántas veces hubo tormenta (truenos en el aeropuerto o sus cercanías) en las 3 horas "
                "siguientes en situaciones así, según la probabilidad de lluvia, la lluvia de dos "
                "modelos más y la energía para tormentas (CAPE) que pronostica Open-Meteo, y la hora "
                "del día. Medido contra un año de observaciones en 12 aeropuertos argentinos. "
                f"Con {NEXT_RAIN_STORM_ODDS}% o más, Próxima lluvia o tormenta la cuenta como tormenta"
            ),
            "fuente": _MODELS_SOURCE if current and _has_models(current) else "Open-Meteo: best_match",
            "proximas_horas": [
                {"desde": when.astimezone(_ARG_TZ).isoformat(), "probabilidad": odds} for when, odds in windows
            ],
        }
        if windows:
            when, odds = max(windows, key=lambda window: window[1])
            attrs["maxima"] = odds
            attrs["maxima_desde"] = when.astimezone(_ARG_TZ).isoformat()
        return attrs


class SMNRainTomorrowSensor(_SMNSensorBase):
    """Lluvia mañana: % chance of rain at some point tomorrow (Argentine day).

    From ECMWF's AIFS, the best model for whole days at Argentine airports
    (see daily_rain_odds). `dias` has tomorrow and the next two days, each
    with SMN's own highest probability for the day next to it, so the
    recorder keeps both to compare them later.
    """

    _attr_translation_key = "rain_tomorrow"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:weather-rainy"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_rain_tomorrow")

    def _days(self) -> list[dict[str, Any]]:
        data = self.coordinator.data
        days = daily_rain_odds(data.open_meteo_models, datetime.now(_ARG_TZ))
        smn: dict[str, int] = {}
        for period in data.hourly_forecast:
            prob = period.get("rain_prob_range")
            if prob and period.get("date"):
                smn[period["date"]] = max(smn.get(period["date"], 0), max(prob))
        return [{**day, "probabilidad_smn": smn.get(day["fecha"])} for day in days]

    @property
    def native_value(self) -> int | None:
        days = self._days()
        tomorrow = (datetime.now(_ARG_TZ) + timedelta(days=1)).date().isoformat()
        return days[0]["probabilidad"] if days and days[0]["fecha"] == tomorrow else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "criterio": (
                "Cuántas veces llovió algo en el día en situaciones así, según la lluvia que "
                "pronostica el modelo AIFS de ECMWF para todo el día (el que más acertó en aeropuertos "
                "argentinos a 1, 2 y 3 días). `probabilidad_smn` es la más alta que da el SMN ese día"
            ),
            "fuente": "Open-Meteo: ECMWF AIFS",
            "dias": self._days(),
        }


class SMNShortTermSummarySensor(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity
):
    """Human-readable short-term forecast/alert summary, in Spanish."""

    _attr_has_entity_name = True
    _attr_translation_key = "short_term_summary"
    _attr_icon = "mdi:text-box-outline"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_short_term_summary"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        state, _ = build_summary(
            data.shortterm_alerts,
            data.alerts,
            data.heat_warnings,
            data.cold_warnings,
            data.daily_forecast,
        )
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        _, attrs = build_summary(
            data.shortterm_alerts,
            data.alerts,
            data.heat_warnings,
            data.cold_warnings,
            data.daily_forecast,
        )
        return attrs


class SMNNationwideAvisosSensor(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity
):
    """Avisos a muy corto plazo for the whole country, grouped by province.

    Same view as smn.gob.ar's "Resumen por provincia" — not filtered to
    the configured location, so it's shared/identical across every SMN
    config entry in this Home Assistant instance.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "nationwide_avisos"
    _attr_icon = "mdi:map-marker-alert-outline"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_nationwide_avisos"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        state, _ = build_nationwide_summary(self.coordinator.data.nationwide_shortterm_alerts)
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, attrs = build_nationwide_summary(self.coordinator.data.nationwide_shortterm_alerts)
        return attrs
