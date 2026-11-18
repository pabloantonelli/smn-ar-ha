"""The Resumen's forecast, told in Spanish and fitted to the time of day.

By SMN's periods (madrugada 0-6, mañana 6-12, tarde 12-18, noche 18-24):
until 18:00 the rest of today plus a line on tomorrow; from 18:00 tonight
plus tomorrow period by period. Each period says what matters: the sky, the
temperature, the rain and storm chances (the integration's calibrated ones,
not the models' raw numbers), and the wind only when it's strong.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any

_ARG_TZ = timezone(timedelta(hours=-3))
_HOUR = timedelta(hours=1)
_PERIODS = ((0, "madrugada"), (6, "mañana"), (12, "tarde"), (18, "noche"))
_WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
# An hour where the rain starts: the same as confirming an SMN period
# (const.NEXT_RAIN_REFINE_*).
_ONSET_PROBABILITY = 30
_ONSET_PRECIPITATION = 0.1
# Wind worth mentioning, km/h.
_WINDY_SPEED = 30
_WINDY_GUST = 45


@dataclass
class Period:
    """What one period (or what's left of it) looks like."""

    name: str
    start: datetime
    end: datetime
    temp_min: float
    temp_max: float
    max_hour: datetime
    feels_at_max: float | None
    cloud: float | None
    rain: int | None
    onset: datetime | None
    storm: int | None
    storm_from: datetime | None
    smn_storm: bool
    wind: float | None
    gust: float | None
    wind_from: str | None


def _round(value: float) -> int:
    """Half up, the way people read temperatures (round() would give 22 for 22.5)."""
    return int(math.floor(value + 0.5))


def _local(when: datetime) -> datetime:
    return when.astimezone(_ARG_TZ)


def _clock(when: datetime) -> str:
    return f"{_local(when).hour}"


def _period_start(when: datetime) -> datetime:
    local = _local(when)
    return local.replace(hour=local.hour // 6 * 6, minute=0, second=0, microsecond=0)


def _period_name(start: datetime) -> str:
    return dict(_PERIODS)[_local(start).hour]


def describe_period(
    start: datetime,
    end: datetime,
    hourly: list[dict[str, Any]],
    smn_periods: dict[datetime, str | None],
) -> Period | None:
    """A Period from Open-Meteo's hours in [start, end), None if it has none.

    The high also takes the hour at `end`: the afternoon's is often at
    18:00 itself.
    """
    from .const import wind_cardinal
    from .sensor import rain_curve, storm_odds

    hours = [h for h in hourly if start <= h["when"] < end and h.get("temperature") is not None]
    if not hours:
        return None
    temperatures = hours + [h for h in hourly if h["when"] == end and h.get("temperature") is not None]
    hottest = max(temperatures, key=lambda h: h["temperature"])
    curve = rain_curve(hourly, hours[0]["when"])
    rain = next((pct for until, pct in reversed(curve) if until <= end), None)
    onset = next(
        (
            h["when"]
            for h in hours
            if (h.get("probability") or 0) >= _ONSET_PROBABILITY
            and (h.get("precipitation") or 0) >= _ONSET_PRECIPITATION
        ),
        None,
    )
    storm, storm_from = None, None
    for h in hours[::3]:
        odds = storm_odds(hourly, h["when"])
        if odds is not None and (storm is None or odds > storm):
            storm, storm_from = odds, h["when"]
    clouds = [h["cloud_coverage"] for h in hours if h.get("cloud_coverage") is not None]
    winds = [h for h in hours if h.get("wind_speed") is not None]
    windiest = max(winds, key=lambda h: h["wind_speed"]) if winds else None
    gusts = [h["wind_gust_speed"] for h in hours if h.get("wind_gust_speed") is not None]
    return Period(
        name=_period_name(_period_start(start)),
        start=start,
        end=end,
        temp_min=min(h["temperature"] for h in hours),
        temp_max=hottest["temperature"],
        max_hour=hottest["when"],
        feels_at_max=hottest.get("apparent_temperature"),
        cloud=sum(clouds) / len(clouds) if clouds else None,
        rain=rain,
        onset=onset,
        storm=storm,
        storm_from=storm_from,
        smn_storm=smn_periods.get(_period_start(start)) == "lightning-rainy",
        wind=windiest["wind_speed"] if windiest else None,
        gust=max(gusts) if gusts else None,
        wind_from=wind_cardinal(windiest.get("wind_bearing")) if windiest else None,
    )


def _sky(periods: list[Period]) -> str | None:
    clouds = [p.cloud for p in periods if p.cloud is not None]
    if not clouds:
        return None
    cloud = sum(clouds) / len(clouds)
    if cloud < 20:
        return "despejado"
    if cloud < 45:
        return "con algunas nubes"
    if cloud < 75:
        return "parcialmente nublado"
    return "nublado"


def _when(period: Period, onset: bool = True) -> str:
    """"desde las 15", or "a la tarde"."""
    if onset and period.onset is not None and period.onset > period.start:
        return f"desde las {_clock(period.onset)}"
    return f"a la {period.name}"


def _rain_words(chance: int | None) -> str | None:
    """The chance of rain in words; None when it's too low to mention."""
    if chance is None or chance < 15:
        return None
    if chance < 35:
        return "no se descarta alguna lluvia"
    if chance < 60:
        return "puede llover"
    if chance < 85:
        return "probablemente llueva"
    return "lluvia casi segura"


def _storm_words(periods: list[Period]) -> tuple[str, Period] | None:
    """One answer for storms: the integration's chance, hedged when SMN sees one it doesn't."""
    stormiest = max((p for p in periods if p.storm is not None), key=lambda p: p.storm, default=None)
    if stormiest is not None and stormiest.storm >= 50:
        return "con tormentas", stormiest
    if stormiest is not None and stormiest.storm >= 25:
        return "con posibles tormentas", stormiest
    smn = next((p for p in periods if p.smn_storm), None)
    if smn is not None:
        return "no se descartan tormentas", smn
    return None


def _weather(periods: list[Period], chance: int | None = None) -> tuple[str | None, str | None]:
    """(sentence about rain and storms, short words for the headline) over some periods."""
    wettest = max((p for p in periods if p.rain is not None), key=lambda p: p.rain, default=None)
    if chance is None and wettest is not None:
        chance = wettest.rain
    rain = _rain_words(chance)
    storm = _storm_words(periods)
    several = len(periods) > 1
    parts = []
    if rain:
        # When: the hour it starts, or the period (only if there are several to tell apart).
        timing = ""
        if wettest is not None and (several or (chance >= 35 and wettest.onset and wettest.onset > wettest.start)):
            timing = f" {_when(wettest, onset=chance >= 35)}"
        if rain == "lluvia casi segura" and timing:
            timing = timing.replace(" a la", ", sobre todo a la").replace(" desde", ", desde")
        parts.append(rain + timing)
    if storm:
        words, period = storm
        when = f" a la {period.name}" if several and not (wettest is period and rain) else ""
        if words == "no se descartan tormentas":
            parts.append(words + when)
        else:
            parts.append((words if rain else words.removeprefix("con ")) + when)
    if not parts:
        return None, None
    if len(parts) == 1:
        text = parts[0]
    elif rain == "no se descarta alguna lluvia" and storm[0] == "no se descartan tormentas":
        text = "no se descartan lluvias ni tormentas" + parts[1].removeprefix("no se descartan tormentas")
    elif parts[1].startswith("no se"):
        text = f"{parts[0]} y {parts[1]}"
    else:
        text = f"{parts[0]}, {parts[1]}"
    return text[0].upper() + text[1:] + ".", _headline_words(rain, storm[0] if storm else None)


_RAIN_SHORT = {
    "no se descarta alguna lluvia": "alguna lluvia",
    "puede llover": "puede llover",
    "probablemente llueva": "probable lluvia",
    "lluvia casi segura": "lluvia",
}


def _headline_words(rain: str | None, storm: str | None) -> str:
    """Short words for the state: "lluvia, posibles tormentas"."""
    words = [w for w in (_RAIN_SHORT.get(rain or ""), (storm or "").removeprefix("con ")) if w]
    return ", ".join(words)


def _wind_words(periods: list[Period]) -> str | None:
    """Strong wind, when there is: "Viento fuerte, con ráfagas de 60 km/h"."""
    windy = [p for p in periods if (p.wind or 0) >= _WINDY_SPEED or (p.gust or 0) >= _WINDY_GUST]
    if not windy:
        return None
    gust = max((p.gust or 0) for p in windy)
    return f"Viento fuerte, con ráfagas de {_round(gust)} km/h." if gust else "Viento fuerte."


def _degrees(value: float | None) -> str | None:
    return None if value is None else f"{_round(value)} °C"


def _smn_day(daily: list[dict[str, Any]], day: Any) -> dict[str, Any]:
    return next((d for d in daily if d.get("date") == day.isoformat()), {})


def _periods_between(now: datetime, until: datetime, hourly, smn_periods) -> list[Period]:
    periods = []
    start = _period_start(now)
    current_hour = _local(now).replace(minute=0, second=0, microsecond=0)
    while start < until:
        end = start + timedelta(hours=6)
        period = describe_period(max(start, current_hour), end, hourly, smn_periods)
        if period is not None:
            periods.append(period)
        start = end
    return periods


def build_forecast(
    now: datetime,
    hourly: list[dict[str, Any]],
    smn_hourly: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    rain_days: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]] | None:
    """(headline, parts) where each part is {"titulo": "Hoy", "texto": ...}; None without Open-Meteo.

    One voice: the integration's own chances decide, told in words; when SMN
    forecasts storms the chance doesn't see, "no se descartan tormentas".
    The day's high and low are SMN's (the same the other cards show); the
    hours (Open-Meteo) only say when the rain starts.
    """
    from .weather import format_condition

    if not hourly:
        return None
    smn_periods: dict[datetime, str | None] = {}
    for period in smn_hourly:
        try:
            when = datetime.strptime(f"{period.get('date')} {period.get('time')}", "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
        smn_periods[when.replace(tzinfo=_ARG_TZ)] = format_condition(period.get("weather"))

    local_now = _local(now)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    evening_start = today_start + timedelta(hours=18)
    midnight = today_start + timedelta(days=1)
    smn_today = _smn_day(daily, today_start.date())
    smn_tomorrow = _smn_day(daily, midnight.date())
    parts: list[dict[str, str]] = []
    headline = None

    if local_now < evening_start:
        day = _periods_between(now, evening_start, hourly, smn_periods)
        if day:
            high = smn_today.get("temp_max")
            high = high if high is not None else max(p.temp_max for p in day)
            sky = _sky(day)
            weather, short = _weather(day)
            text = f"{sky.capitalize()}, hasta {_degrees(high)}." if sky else f"Hasta {_degrees(high)}."
            if weather:
                text += f" {weather}"
            else:
                text = text[:-1] + ", sin lluvia."
            if wind := _wind_words(day):
                text += f" {wind}"
            parts.append({"titulo": "Hoy", "texto": text})
            low = smn_today.get("temp_min")
            span = f"{_round(low)} a {_degrees(high)}" if low is not None else f"hasta {_degrees(high)}"
            headline = f"Hoy: {short or sky or 'sin lluvia'} · {span}"

    night = _periods_between(max(now, evening_start), midnight, hourly, smn_periods)
    if night:
        low = smn_tomorrow.get("temp_min")
        low = low if low is not None else min(p.temp_min for p in night)
        sky = _sky(night)
        weather, _ = _weather(night)
        text = f"{sky.capitalize()}, mínima de {_degrees(low)}." if sky else f"Mínima de {_degrees(low)}."
        if weather:
            text += f" {weather}"
        if wind := _wind_words(night):
            text += f" {wind}"
        parts.append({"titulo": "Esta noche", "texto": text})

    tomorrow = _periods_between(midnight + timedelta(hours=6), midnight + timedelta(days=1), hourly, smn_periods)
    if tomorrow:
        rain_day = next((d for d in rain_days if d.get("fecha") == midnight.date().isoformat()), None)
        weather, short = _weather(tomorrow, rain_day["probabilidad"] if rain_day else None)
        sky = _sky(tomorrow)
        low, high = smn_tomorrow.get("temp_min"), smn_tomorrow.get("temp_max")
        if low is None or high is None:
            low, high = min(p.temp_min for p in tomorrow), max(p.temp_max for p in tomorrow)
        span = f"Entre {_round(low)} y {_degrees(high)}."
        if weather:
            text = f"{weather} {span}"
        else:
            text = f"{(sky or 'sin lluvia').capitalize()}{' y sin lluvia' if sky else ''}. {span}"
        if wind := _wind_words(tomorrow):
            text += f" {wind}"
        weekday = _WEEKDAYS[midnight.weekday()]
        parts.append({"titulo": f"Mañana {weekday}", "texto": text})
        if headline is None:
            headline = f"Mañana: {short or sky or 'sin lluvia'} · {_round(low)} a {_degrees(high)}"

    if headline is None:
        return None
    return headline, parts


def forecast_text(parts: list[dict[str, str]]) -> str:
    """The parts as one text, a line each (for TTS and notifications)."""
    return "\n".join(f"{part['titulo']}: {part['texto'][0].lower()}{part['texto'][1:]}" for part in parts)
