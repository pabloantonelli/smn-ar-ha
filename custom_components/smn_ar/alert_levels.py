"""Shared helpers for reading SMN's zone alerts by time period, and the alert radius.

SMN's warning/alert/location response doesn't give one level per day: each
event has a level per period of the day (early_morning/morning/afternoon/
night, i.e. 00-06/06-12/12-18/18-24 Argentina time), plus `max_level` for
the whole day. Using `max_level` keeps an alert on for 24h even when SMN
only forecasts it for the afternoon, so the zone alerts use the level of
the current period instead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from typing import Any
import unicodedata

from homeassistant.config_entries import ConfigEntry

from .const import CONF_ALERT_RADIUS_KM, CONF_HAIL_RADIUS_KM, DEFAULT_ALERT_RADIUS_KM

ARG_TZ = timezone(timedelta(hours=-3))

# (period key in the API, start hour in Argentina time, Spanish label)
PERIODS: tuple[tuple[str, int, str], ...] = (
    ("early_morning", 0, "madrugada"),
    ("morning", 6, "mañana"),
    ("afternoon", 12, "tarde"),
    ("night", 18, "noche"),
)
# UTC hours at which a new period starts in Argentina (UTC-3, no DST).
PERIOD_START_UTC_HOURS = [(start + 3) % 24 for _, start, _ in PERIODS]


def get_alert_radius_km(entry: ConfigEntry) -> float:
    """Radius for the proximity-based alerts, from the integration's options."""
    options = entry.options
    return float(
        options.get(
            CONF_ALERT_RADIUS_KM,
            options.get(CONF_HAIL_RADIUS_KM, DEFAULT_ALERT_RADIUS_KM),
        )
    )


def current_period(now: datetime | None = None) -> tuple[str, str, str]:
    """(today's date as YYYY-MM-DD, period key, Spanish label) in Argentina time."""
    local = (now or datetime.now(timezone.utc)).astimezone(ARG_TZ)
    key, label = PERIODS[0][0], PERIODS[0][2]
    for period_key, start, period_label in PERIODS:
        if local.hour >= start:
            key, label = period_key, period_label
    return local.date().isoformat(), key, label


def todays_warning(alerts: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any] | None:
    """The `warnings` entry for today (Argentina time), or None if there isn't one."""
    today, _, _ = current_period(now)
    for warning in (alerts or {}).get("warnings") or []:
        if warning.get("date") == today:
            return warning
    return None


def event_levels(
    alerts: dict[str, Any] | None, event_id: int, now: datetime | None = None
) -> tuple[int, int, dict[str, int | None]]:
    """(level now, max level today, levels by period) for one event in today's warning."""
    warning = todays_warning(alerts, now)
    if warning is None:
        return 1, 1, {}
    _, period, _ = current_period(now)
    for event in warning.get("events", []):
        if event.get("id") == event_id:
            levels = event.get("levels") or {}
            return levels.get(period) or 1, event.get("max_level", 1), levels
    return 1, 1, {}


def level_report(alerts: dict[str, Any] | None, event_id: int, level: int) -> tuple[str | None, str | None]:
    """(description, instruction) SMN publishes for an event at a given level."""
    for report in (alerts or {}).get("reports") or []:
        if report.get("event_id") != event_id:
            continue
        for level_data in report.get("levels", []):
            if level_data.get("level") == level:
                return level_data.get("description"), level_data.get("instruction")
    return None, None


def alert_periods(
    alerts: dict[str, Any] | None, event_id: int, now: datetime | None = None
) -> list[tuple[datetime, int]]:
    """(period start, level) of every current or upcoming period with an alert (level > 1) for an event.

    Covers every day in the response (today plus the next couple of days).
    """
    now = now or datetime.now(timezone.utc)
    found = []
    for warning in (alerts or {}).get("warnings") or []:
        try:
            day = datetime.strptime(warning.get("date") or "", "%Y-%m-%d").replace(tzinfo=ARG_TZ)
        except ValueError:
            continue
        for event in warning.get("events", []):
            if event.get("id") != event_id:
                continue
            levels = event.get("levels") or {}
            for key, start_hour, _ in PERIODS:
                level = levels.get(key) or 1
                start = day + timedelta(hours=start_hour)
                if level > 1 and start + timedelta(hours=6) > now:
                    found.append((start, level))
    found.sort()
    return found


def _normalized_title(aviso: dict[str, Any]) -> str:
    decomposed = unicodedata.normalize("NFKD", aviso.get("title") or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).upper()


def aviso_matches(
    aviso: dict[str, Any], keywords: tuple[tuple[str, ...], tuple[str, ...]] | None
) -> bool:
    """Whether an aviso's title matches (include, exclude) keywords; None matches any aviso."""
    if keywords is None:
        return True
    include, exclude = keywords
    title = _normalized_title(aviso)
    return any(k in title for k in include) and not any(k in title for k in exclude)


def distance_to_aviso_km(
    latitude: float, longitude: float, aviso: dict[str, Any]
) -> tuple[float, float] | None:
    """(distance in km, bearing in degrees) from a point to an aviso's polygon.

    0 km when the point is inside it. Uses a local equirectangular
    projection around the point — accurate to well under 1% at the few
    hundred km this is used for.
    """
    rings = ((aviso.get("geometry") or {}).get("coordinates")) or []
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(latitude))
    best: tuple[float, float] | None = None
    for ring in rings:
        points = [((lon - longitude) * km_per_deg_lon, (lat - latitude) * km_per_deg_lat) for lon, lat in ring]
        if len(points) < 3:
            continue
        inside = False
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            if (y0 > 0) != (y1 > 0) and 0 < x0 + (0 - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
            dx, dy = x1 - x0, y1 - y0
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(x0 * dx + y0 * dy) / length_sq))
            nx, ny = x0 + t * dx, y0 + t * dy
            distance = math.hypot(nx, ny)
            if best is None or distance < best[0]:
                best = (distance, math.degrees(math.atan2(nx, ny)) % 360)
        if inside:
            return 0.0, 0.0
    return best


def ranked_avisos(
    latitude: float,
    longitude: float,
    avisos: list[dict[str, Any]] | None,
    keywords: tuple[tuple[str, ...], tuple[str, ...]] | None,
) -> list[tuple[float, float, dict[str, Any]]]:
    """Avisos matching keywords as (distance km, bearing, aviso), nearest first."""
    ranked = []
    for aviso in avisos or []:
        if not aviso_matches(aviso, keywords):
            continue
        result = distance_to_aviso_km(latitude, longitude, aviso)
        if result:
            ranked.append((result[0], result[1], aviso))
    ranked.sort(key=lambda item: item[0])
    return ranked


def active_events_now(alerts: dict[str, Any] | None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Today's events with an alert (level > 1) in the current period.

    Each item has event_id, level (now), max_level (today) and levels (by
    period).
    """
    warning = todays_warning(alerts, now)
    if warning is None:
        return []
    _, period, _ = current_period(now)
    active = []
    for event in warning.get("events", []):
        levels = event.get("levels") or {}
        level = levels.get(period) or 1
        if level > 1:
            active.append(
                {
                    "event_id": event.get("id"),
                    "level": level,
                    "max_level": event.get("max_level", level),
                    "levels": levels,
                }
            )
    return active
