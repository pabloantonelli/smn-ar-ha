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
