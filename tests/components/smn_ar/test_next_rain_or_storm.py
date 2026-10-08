"""Test "Próxima lluvia o tormenta" (find_next_rain_or_storm)."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smn_ar.sensor import find_next_rain_or_storm

ARG = timezone(timedelta(hours=-3))
NOW = datetime(2026, 10, 7, 8, 0, tzinfo=ARG)  # morning period
LAT, LON = -34.6217, -58.4258

STORM_AVISO_20KM = {
    "title": "TORMENTAS FUERTES CON LLUVIAS INTENSAS. ",
    "date": "2026-10-07T07:30:00-03:00",
    "end_date": "2026-10-07T09:30:00-03:00",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[-58.50, -34.80], [-58.35, -34.80], [-58.35, -34.90], [-58.50, -34.90]]],
    },
}


def _period(time: str, weather_id: int, prob: list[int], date: str = "2026-10-07") -> dict:
    return {"date": date, "time": time, "weather": {"id": weather_id}, "rain_prob_range": prob}


def _alerts(event_id: int, levels: dict) -> dict:
    return {
        "warnings": [
            {"date": "2026-10-07", "events": [{"id": event_id, "max_level": max(v or 1 for v in levels.values()), "levels": levels}]}
        ]
    }


def _find(hourly=(), alerts=None, avisos=None, radius=30):
    return find_next_rain_or_storm(list(hourly), alerts, avisos, LAT, LON, radius, NOW)


def test_nothing_forecast() -> None:
    assert _find([_period("12:00", 3, [0, 10])]) == (None, None)


def test_forecast_rain() -> None:
    next_any, next_storm = _find([_period("12:00", 73, [40, 70])])
    assert next_any["tipo"] == "lluvia"
    assert next_any["source"][1] == "pronóstico"
    assert next_any["when"] == datetime(2026, 10, 7, 12, 0, tzinfo=ARG)
    assert next_any["probability"] == 70
    assert next_storm is None


def test_forecast_storm_counts_even_with_low_probability() -> None:
    next_any, next_storm = _find([_period("18:00", 76, [10, 20])])
    assert next_any["tipo"] == "tormenta"
    assert next_storm is next_any


def test_zone_alert_wins_same_period_and_keeps_probability() -> None:
    hourly = [_period("12:00", 73, [40, 70])]
    alerts = _alerts(41, {"early_morning": None, "morning": 1, "afternoon": 4, "night": 1})
    next_any, _ = _find(hourly, alerts)
    assert next_any["source"][1] == "alerta de zona"
    assert next_any["tipo"] == "tormenta"
    assert next_any["level_name"] == "naranja"
    assert next_any["probability"] == 70


def test_zone_alert_in_current_period() -> None:
    alerts = _alerts(37, {"early_morning": None, "morning": 3, "afternoon": 1, "night": 1})
    next_any, _ = _find([_period("12:00", 81, [70, 100])], alerts)
    assert next_any["source"][1] == "alerta de zona"
    assert next_any["when"] == datetime(2026, 10, 7, 6, 0, tzinfo=ARG)  # under way


def test_nearby_aviso_beats_alert_under_way() -> None:
    alerts = _alerts(41, {"early_morning": None, "morning": 3, "afternoon": 1, "night": 1})
    next_any, _ = _find(alerts=alerts, avisos=[STORM_AVISO_20KM])
    assert next_any["source"][1] == "aviso cercano"
    assert next_any["distance_km"] == pytest.approx(19.7, abs=0.5)


def test_aviso_outside_radius_is_ignored() -> None:
    next_any, _ = _find([_period("18:00", 73, [40, 70])], avisos=[STORM_AVISO_20KM], radius=10)
    assert next_any["source"][1] == "pronóstico"


def test_next_storm_after_rain() -> None:
    hourly = [_period("12:00", 73, [40, 70]), _period("06:00", 89, [70, 100], date="2026-10-08")]
    next_any, next_storm = _find(hourly)
    assert next_any["tipo"] == "lluvia"
    assert next_storm["when"] == datetime(2026, 10, 8, 6, 0, tzinfo=ARG)


async def test_entity_attributes(hass, enable_custom_integrations, freezer) -> None:
    """The sensor exposes the combined result with tipo/fuente/criterio."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
    from homeassistant.helpers import entity_registry as er

    from custom_components.smn_ar.const import DOMAIN

    freezer.move_to("2026-10-07 11:00:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON, CONF_NAME: "Casa"},
        unique_id="4864",
    )
    entry.add_to_hass(hass)

    async def fake_fetch(self) -> None:
        self.hourly_forecast = [_period("12:00", 73, [40, 70]), _period("18:00", 81, [40, 70])]

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.SENSOR]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_next_rain")
    state = hass.states.get(entity_id)
    assert state.state == "2026-10-07T15:00:00+00:00"
    assert state.attributes["tipo"] == "lluvia"
    assert state.attributes["fuente"] == "pronóstico"
    assert state.attributes["en_curso"] is False
    assert state.attributes["proxima_tormenta"] == "2026-10-07T18:00:00-03:00"
    assert "30 km" in state.attributes["criterio"]


def _hour(hour: int, probability: int, precipitation: float = 1.0, code: int = 61) -> dict:
    return {
        "when": datetime(2026, 10, 7, hour, 0, tzinfo=ARG),
        "probability": probability,
        "precipitation": precipitation,
        "weather_code": code,
        "wind_speed_700hpa": 40,
        "wind_direction_700hpa": 270,  # from the W: storms move east
    }


def _find_with(hourly=(), alerts=None, avisos=None, radius=30, open_meteo_hourly=None, nowcast=None):
    return find_next_rain_or_storm(
        list(hourly), alerts, avisos, LAT, LON, radius, NOW,
        open_meteo_hourly=open_meteo_hourly, nowcast=nowcast,
    )


def test_low_probability_range_no_longer_counts() -> None:
    assert _find([_period("12:00", 73, [10, 30])]) == (None, None)


def test_hourly_forecast_refines_the_smn_period() -> None:
    open_meteo_hourly = [_hour(h, 5, 0.0) for h in range(8, 15)] + [_hour(15, 30, 0.3)]
    next_any, _ = _find_with([_period("12:00", 73, [40, 70])], open_meteo_hourly=open_meteo_hourly)
    assert next_any["source"][1] == "pronóstico"
    assert next_any["when"] == datetime(2026, 10, 7, 15, 0, tzinfo=ARG)
    assert next_any["probability"] == 70  # still the SMN period's


def test_hourly_forecast_alone() -> None:
    open_meteo_hourly = [_hour(9, 10, 0.0), _hour(10, 60, 2.0), _hour(11, 50, 1.0, code=95)]
    next_any, next_storm = _find_with(open_meteo_hourly=open_meteo_hourly)
    assert next_any["source"][1] == "pronóstico horario"
    assert next_any["when"] == datetime(2026, 10, 7, 10, 0, tzinfo=ARG)
    assert next_storm["when"] == datetime(2026, 10, 7, 11, 0, tzinfo=ARG)


def test_nowcast_wins_with_its_details() -> None:
    from custom_components.smn_ar.nowcast import SOURCE_SATELLITE, Motion, Nowcast

    nowcast = Nowcast(
        source=SOURCE_SATELLITE,
        frame_time=NOW - timedelta(minutes=20),
        horizon_end=NOW + timedelta(minutes=100),
        motion=Motion(45, 120, SOURCE_SATELLITE),
        arrival=NOW + timedelta(minutes=35),
        until=NOW + timedelta(minutes=80),
        tipo="tormenta",
        intensity=-64,
        distance_km=41,
        from_deg=300,
    )
    next_any, next_storm = _find_with([_period("12:00", 89, [40, 70])], nowcast=nowcast)
    assert next_any is next_storm
    assert next_any["source"][1] == "satélite infrarrojo"
    assert next_any["when"] == NOW + timedelta(minutes=35)
    assert next_any["direction"] == "WNW"
    assert next_any["motion"].speed_kmh == 45


def test_aviso_carried_by_the_wind() -> None:
    # The aviso's area is ~20 km S of the location: storms moving east miss it.
    open_meteo_hourly = [_hour(h, 0, 0.0) for h in range(6, 12)]
    next_any, _ = _find_with(avisos=[STORM_AVISO_20KM], open_meteo_hourly=open_meteo_hourly)
    assert next_any is None
    # Moving north (wind from the S), it gets here ~30 min after it was issued.
    for hour in open_meteo_hourly:
        hour["wind_direction_700hpa"] = 180
    next_any, _ = _find_with(avisos=[STORM_AVISO_20KM], open_meteo_hourly=open_meteo_hourly)
    assert next_any["source"][1] == "aviso cercano"
    issued = datetime(2026, 10, 7, 7, 30, tzinfo=ARG)
    assert issued + timedelta(minutes=15) <= next_any["when"] <= issued + timedelta(minutes=40)


def test_radar_overrides_forecasts_within_its_horizon() -> None:
    from custom_components.smn_ar.nowcast import SOURCE_RADAR, Nowcast

    horizon_end = NOW + timedelta(minutes=100)
    dry_radar = Nowcast(source=SOURCE_RADAR, frame_time=NOW - timedelta(minutes=20), horizon_end=horizon_end, motion=None)
    open_meteo_hourly = [_hour(8, 80, 3.0, code=95)]
    next_any, _ = _find_with(open_meteo_hourly=open_meteo_hourly, nowcast=dry_radar)
    assert next_any["when"] == horizon_end
    assert next_any["source"][1] == "pronóstico horario"


def test_stale_or_finished_nowcast_is_ignored() -> None:
    from custom_components.smn_ar.nowcast import SOURCE_SATELLITE, Nowcast

    def nowcast(frame_age: int, until: int | None = None) -> Nowcast:
        frame_time = NOW - timedelta(minutes=frame_age)
        return Nowcast(
            source=SOURCE_SATELLITE,
            frame_time=frame_time,
            horizon_end=frame_time + timedelta(minutes=120),
            motion=None,
            arrival=frame_time,
            until=None if until is None else NOW + timedelta(minutes=until),
            tipo="lluvia",
        )

    assert _find_with(nowcast=nowcast(30))[0]["source"][1] == "satélite infrarrojo"
    assert _find_with(nowcast=nowcast(180)) == (None, None)  # the source kept failing
    assert _find_with(nowcast=nowcast(30, until=-5)) == (None, None)  # already went past
