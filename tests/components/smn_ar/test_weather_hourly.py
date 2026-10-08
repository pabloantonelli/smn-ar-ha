"""Test the weather entity's hourly forecast from Open-Meteo."""
from datetime import datetime, timedelta, timezone

from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_SUNNY,
)

from custom_components.smn_ar.weather import open_meteo_hourly_forecast

NOW = datetime(2026, 10, 8, 14, 20, tzinfo=timezone.utc)


def _hour(hours_from_13: int, code: int, is_day: int = 1) -> dict:
    return {
        "when": datetime(2026, 10, 8, 13, tzinfo=timezone.utc) + timedelta(hours=hours_from_13),
        "weather_code": code,
        "is_day": is_day,
        "temperature": 21.5,
        "apparent_temperature": 20.0,
        "humidity": 60,
        "precipitation": 1.2,
        "probability": 70,
        "wind_speed": 15.0,
        "wind_gust_speed": 30.0,
        "wind_bearing": 200,
        "cloud_coverage": 90,
    }


def test_hourly_from_the_current_hour() -> None:
    hourly = [_hour(0, 0), _hour(1, 0), _hour(2, 95), _hour(10, 0, is_day=0)]
    forecast = open_meteo_hourly_forecast(hourly, NOW)
    assert [f["datetime"] for f in forecast] == [
        "2026-10-08T14:00:00+00:00",
        "2026-10-08T15:00:00+00:00",
        "2026-10-08T23:00:00+00:00",
    ]
    assert [f["condition"] for f in forecast] == [
        ATTR_CONDITION_SUNNY,
        ATTR_CONDITION_LIGHTNING_RAINY,
        ATTR_CONDITION_CLEAR_NIGHT,
    ]
    assert forecast[0]["precipitation_probability"] == 70
    assert forecast[0]["native_precipitation"] == 1.2
    assert forecast[0]["wind_bearing"] == 200


def test_trace_drizzle_shows_the_clouds() -> None:
    drizzle = _hour(1, 51)
    drizzle.update(probability=7, precipitation=0.1, cloud_coverage=100)
    rain = _hour(2, 61)
    forecast = open_meteo_hourly_forecast([drizzle, rain], NOW)
    assert [f["condition"] for f in forecast] == ["cloudy", "rainy"]
