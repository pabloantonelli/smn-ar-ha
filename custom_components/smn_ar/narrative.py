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
_DIRECTIONS = {
    "N": "norte", "NNE": "nornoreste", "NE": "noreste", "ENE": "estenoreste",
    "E": "este", "ESE": "estesureste", "SE": "sureste", "SSE": "sursureste",
    "S": "sur", "SSW": "sursuroeste", "SW": "suroeste", "WSW": "oestesuroeste",
    "W": "oeste", "WNW": "oestenoroeste", "NW": "noroeste", "NNW": "nornoroeste",
}
# An hour where the rain starts: the same as confirming an SMN period
# (const.NEXT_RAIN_REFINE_*).
_ONSET_PROBABILITY = 30
_ONSET_PRECIPITATION = 0.1
# Wind worth mentioning, km/h.
_WINDY_SPEED = 30
_WINDY_GUST = 45
# Feels-like worth mentioning: this many °C away from the temperature.
_FEELS_LIKE_GAP = 3


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


def _sky(period: Period) -> str | None:
    if period.cloud is None:
        return None
    if period.cloud < 20:
        return "despejada"
    if period.cloud < 45:
        return "con pocas nubes"
    if period.cloud < 75:
        return "parcialmente nublada"
    return "nublada"


def _temperature(period: Period) -> str:
    low, high = _round(period.temp_min), _round(period.temp_max)
    if period.name == "tarde":
        text = f"con máxima de {high} °C a las {_clock(period.max_hour)}"
    elif period.name == "mañana":
        text = f"con {low} °C" if low == high else f"entre {low} y {high} °C"
    else:
        text = f"con mínima de {low} °C"
    feels = period.feels_at_max
    if feels is not None and period.name in ("mañana", "tarde") and abs(feels - period.temp_max) >= _FEELS_LIKE_GAP:
        text += f" (sensación de {_round(feels)} °C)"
    return text


def _rain(period: Period) -> str | None:
    rain = period.rain
    if rain is None or rain < 15:
        return None
    since = f" desde las {_clock(period.onset)}" if period.onset and period.onset > period.start else ""
    if rain >= 85:
        return f"lluvia casi segura{since} ({rain} %)"
    if rain >= 60:
        return f"probable lluvia{since} ({rain} %)"
    if rain >= 35:
        return f"puede llover{since} ({rain} %)"
    return f"baja chance de lluvia ({rain} %)"


def _storm(period: Period, smn_note: bool = True) -> str | None:
    storm = period.storm
    if storm is None:
        return None
    window = ""
    if period.storm_from is not None:
        window = f" entre las {_clock(period.storm_from)} y las {_clock(period.storm_from + 3 * _HOUR)}"
    if storm >= 50:
        return f"tormentas probables ({storm} %{window})"
    if storm >= 25:
        return f"posibles tormentas ({storm} %{window})"
    if period.smn_storm and smn_note:
        return f"el SMN anuncia tormentas, aunque por ahora se ven poco probables ({storm} %)"
    return None


def _wind(period: Period) -> str | None:
    if period.wind is None:
        return None
    windy = period.wind >= _WINDY_SPEED or (period.gust or 0) >= _WINDY_GUST
    if not windy:
        return None
    text = f"Viento del {_DIRECTIONS.get(period.wind_from or '', 'variable')} de {_round(period.wind)} km/h"
    if period.gust:
        text += f" con ráfagas de {_round(period.gust)}"
    return text


def _smn_only_storm(period: Period) -> bool:
    """SMN says storm, the chance says it's unlikely: the note that tells them apart."""
    return period.smn_storm and (period.storm is None or period.storm < 25)


def _sentences(periods: list[Period], now: datetime, first_is_current: bool, under_day: bool = False) -> list[str]:
    """One sentence per period; the SMN-storm note only the first time it applies."""
    sentences, noted = [], False
    for index, period in enumerate(periods):
        label = _label(period, now, first_is_current and index == 0, under_day)
        sentences.append(_sentence(label, period, smn_note=not noted))
        noted = noted or _smn_only_storm(period)
    return sentences


def _sentence(label: str, period: Period, smn_note: bool = True) -> str:
    sky = _sky(period)
    head = f"{label}, {sky}, {_temperature(period)}." if sky else f"{label}, {_temperature(period)}."
    weather = [part for part in (_rain(period), _storm(period, smn_note)) if part]
    if weather:
        text = " y ".join(weather) if len(weather) == 2 and not weather[1].startswith("el SMN") else "; ".join(weather)
        head += f" {text[0].upper()}{text[1:]}."
    if wind := _wind(period):
        head += f" {wind}."
    return head


def _label(period: Period, now: datetime, current: bool, under_day: bool = False) -> str:
    """"Esta tarde", "A la noche", "Mañana a la tarde"; just "A la tarde" under a day's heading."""
    days = (_local(period.start).date() - _local(now).date()).days
    if days == 0 and current:
        return f"Esta {period.name}"
    if days == 0 or under_day:
        return f"A la {period.name}"
    if days == 1:
        return f"Mañana a la {period.name}"
    return f"El {_WEEKDAYS[_local(period.start).weekday()]} a la {period.name}"


def _periods_between(now: datetime, until: datetime, hourly, smn_periods) -> list[Period]:
    periods = []
    start = _period_start(now)
    while start < until:
        end = start + timedelta(hours=6)
        current_hour = _local(now).replace(minute=0, second=0, microsecond=0)
        period = describe_period(max(start, current_hour), end, hourly, smn_periods)
        if period is not None:
            periods.append(period)
        start = end
    return periods


# (rank, words) for the headline, most notable first.
def _headline_weather(period: Period) -> tuple[int, str] | None:
    if period.storm is not None and period.storm >= 25:
        return 4, "posibles tormentas"
    if period.rain is not None and period.rain >= 60:
        return 3, "lluvia"
    if period.rain is not None and period.rain >= 35:
        return 2, "chance de lluvia"
    if period.rain is not None and period.rain >= 15:
        return 1, "poca chance de lluvia"
    return None


def _rain_words(pct: int) -> str:
    if pct >= 85:
        return "lluvia casi segura"
    if pct >= 60:
        return "probable lluvia"
    if pct >= 35:
        return "puede llover"
    if pct >= 15:
        return "poca chance de lluvia"
    return "sin lluvia"


def _range(periods: list[Period]) -> str | None:
    if not periods:
        return None
    low = _round(min(p.temp_min for p in periods))
    high = _round(max(p.temp_max for p in periods))
    return f"{low} °C" if low == high else f"{low} a {high} °C"


def build_forecast(
    now: datetime,
    hourly: list[dict[str, Any]],
    smn_hourly: list[dict[str, Any]],
    daily: list[dict[str, Any]],
    rain_days: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """(headline, full text), or None without Open-Meteo's hours to tell it from.

    Temperatures all come from Open-Meteo's hours, so the text never says two
    different maxima; SMN adds its condition for tomorrow and its storms.
    """
    from .const import CONDITION_LABELS_ES
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
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    evening = local_now.hour >= 18
    today = _periods_between(now, midnight, hourly, smn_periods)
    whole_tomorrow = _periods_between(midnight, midnight + timedelta(days=1), hourly, smn_periods)
    sentences = _sentences(today, now, first_is_current=True)
    if today and not any(_rain(p) or _storm(p) for p in today):
        sentences.append("Sin lluvia en lo que queda del día." if len(today) > 1 else "Sin lluvia.")

    tomorrow_date = midnight.date()
    weekday = _WEEKDAYS[tomorrow_date.weekday()]
    rain_tomorrow = next((d for d in rain_days if d.get("fecha") == tomorrow_date.isoformat()), None)
    tomorrow_smn = next((d for d in daily if d.get("date") == tomorrow_date.isoformat()), None)
    smn_condition = format_condition(tomorrow_smn.get("weather")) if tomorrow_smn else None
    smn_says = (
        CONDITION_LABELS_ES.get(smn_condition, "").lower()
        if smn_condition in ("lightning-rainy", "pouring", "rainy", "snowy", "snowy-rainy")
        else None
    )
    day_parts = []
    if rain_tomorrow is not None:
        day_parts.append(f"{_rain_words(rain_tomorrow['probabilidad'])} ({rain_tomorrow['probabilidad']} %)")
    if (span := _range(whole_tomorrow)) is not None:
        day_parts.append(f"de {span}")

    daytime_tomorrow = [p for p in whole_tomorrow if p.name != "madrugada"]
    if evening and daytime_tomorrow:
        # Tonight is short; tomorrow by periods, from the morning on.
        by_period = _sentences(daytime_tomorrow, now, first_is_current=False, under_day=True)
        sentences.append(f"Mañana {weekday}: {by_period[0][0].lower()}{by_period[0][1:]}")
        sentences += by_period[1:]
        if not any(_rain(p) or _storm(p) for p in daytime_tomorrow):
            sentences.append("Sin lluvia.")
        if day_parts:
            sentences.append(f"En todo el día, {', '.join(day_parts)}.")
    elif day_parts:
        text = f"Mañana {weekday}: " + ", ".join(day_parts)
        strongest = max((p for p in whole_tomorrow if p.storm is not None), key=lambda p: p.storm, default=None)
        if strongest is not None and strongest.storm >= 25 and strongest.storm_from is not None:
            text += (
                f", con más chance de tormenta entre las {_clock(strongest.storm_from)}"
                f" y las {_clock(strongest.storm_from + 3 * _HOUR)} ({strongest.storm} %)"
            )
        sentences.append(text + ".")
    if smn_says and sentences:
        sentences[-1] = sentences[-1].rstrip(".") + f"; el SMN pronostica {smn_says} para mañana."

    # Headline: the most notable thing ahead (today; tomorrow after 18:00).
    focus = daytime_tomorrow if evening and daytime_tomorrow else today
    ranked = [(w, i, p) for i, p in enumerate(focus) if (w := _headline_weather(p))]
    if ranked:
        (_, words), _, period = max(ranked, key=lambda item: (item[0][0], -item[1]))
        when = _label(period, now, period is focus[0] and not evening)
        headline = f"{when} con {words}"
    else:
        headline = "Mañana sin lluvia" if evening else "Sin lluvia el resto del día"
    if (span := _range(whole_tomorrow if evening else today)) is not None:
        headline += f" · {span}"
    return headline, " ".join(sentences)
