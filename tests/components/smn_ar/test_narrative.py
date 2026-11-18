"""The Resumen's forecast text (narrative.py)."""
from datetime import datetime, timedelta, timezone

from custom_components.smn_ar.narrative import build_forecast, forecast_text

ARG = timezone(timedelta(hours=-3))
UTC = timezone.utc
START = datetime(2026, 10, 7, 0, 0, tzinfo=ARG)  # a Wednesday


def _hours(stormy=(), rainy=(), windy=()) -> list[dict]:
    """Three days of hours: 14 °C at dawn to 26 °C at 16:00, dry and calm unless told."""
    hours = []
    for index in range(72):
        when = START + timedelta(hours=index)
        hour = when.hour
        temperature = 14 + 12 * max(0.0, 1 - abs(hour - 16) / 10)
        storm, rain, wind = when in stormy, when in rainy or when in stormy, when in windy
        hours.append(
            {
                "when": when.astimezone(UTC),
                "temperature": temperature,
                "apparent_temperature": temperature,
                "cloud_coverage": 90 if rain else 10,
                "wind_speed": 35 if wind else 10,
                "wind_gust_speed": 60 if wind else 20,
                "wind_bearing": 45,
                "probability": 95 if rain else 0,
                "precipitation": 3.0 if rain else 0.0,
                "cape": 2500 if storm else 0,
                "precipitation_aifs": 3.0 if rain else 0.0,
                "precipitation_ukmo": 2.0 if rain else 0.0,
            }
        )
    return hours


def _at(day: int, hour: int) -> datetime:
    return START + timedelta(days=day, hours=hour)


SMN_DAYS = [
    {"date": "2026-10-07", "temp_min": 15, "temp_max": 27, "weather": {"id": 3}},
    {"date": "2026-10-08", "temp_min": 16, "temp_max": 28, "weather": {"id": 3}},
]


def _text(now, hours, smn=(), daily=SMN_DAYS, rain_days=()):
    headline, parts = build_forecast(now, hours, list(smn), list(daily), list(rain_days))
    return headline, forecast_text(parts), parts


def test_a_dry_day_in_three_short_parts() -> None:
    headline, text, parts = _text(_at(0, 8), _hours(), rain_days=[{"fecha": "2026-10-08", "probabilidad": 9}])
    assert headline == "Hoy: despejado · 15 a 27 °C"
    assert [p["titulo"] for p in parts] == ["Hoy", "Esta noche", "Mañana jueves"]
    assert text == (
        "Hoy: despejado, hasta 27 °C, sin lluvia.\n"
        "Esta noche: despejado, mínima de 16 °C.\n"
        "Mañana jueves: despejado y sin lluvia. Entre 16 y 28 °C."
    )


def test_no_percentages_and_smn_temperatures() -> None:
    rainy = [_at(0, h) for h in range(14, 18)]
    _, text, _ = _text(_at(0, 9), _hours(rainy=rainy))
    assert "%" not in text
    assert "hasta 27 °C" in text  # SMN's high, not the hours' 26


def test_afternoon_storms() -> None:
    stormy = [_at(0, h) for h in range(15, 19)]
    headline, text, parts = _text(_at(0, 9), _hours(stormy=stormy))
    assert headline.startswith("Hoy: lluvia, tormentas")
    assert parts[0]["texto"] == "Con algunas nubes, hasta 27 °C. Lluvia casi segura, desde las 15, con tormentas."


def test_smn_storm_the_chance_doesnt_see_is_one_hedge() -> None:
    smn = [{"date": "2026-10-07", "time": "12:00", "weather": {"id": 76}}]
    headline, _, parts = _text(_at(0, 9), _hours(), smn=smn)
    assert parts[0]["texto"] == "Despejado, hasta 27 °C. No se descartan tormentas a la tarde."
    assert "SMN" not in parts[0]["texto"] and "modelo" not in parts[0]["texto"]
    assert headline == "Hoy: no se descartan tormentas · 15 a 27 °C"


def test_evening_is_tonight_and_tomorrow() -> None:
    rainy = [_at(1, h) for h in range(13, 18)]
    headline, text, parts = _text(_at(0, 20), _hours(rainy=rainy), rain_days=[{"fecha": "2026-10-08", "probabilidad": 90}])
    assert [p["titulo"] for p in parts] == ["Esta noche", "Mañana jueves"]
    assert headline == "Mañana: lluvia · 16 a 28 °C"
    assert parts[1]["texto"] == "Lluvia casi segura, desde las 13. Entre 16 y 28 °C."


def test_strong_wind_is_mentioned() -> None:
    _, _, parts = _text(_at(0, 13), _hours(windy=[_at(0, 15)]))
    assert parts[0]["texto"].endswith("Viento fuerte, con ráfagas de 60 km/h.")


def test_without_open_meteo_nothing_to_tell() -> None:
    assert build_forecast(_at(0, 8), [], [], [], []) is None


async def test_summary_entity(hass, enable_custom_integrations, freezer) -> None:
    """The headline is the state; an aviso still comes first, with the forecast after it."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
    from homeassistant.helpers import entity_registry as er

    from custom_components.smn_ar.const import DOMAIN

    freezer.move_to(_at(0, 8).astimezone(UTC))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LATITUDE: -31.4, CONF_LONGITUDE: -64.2, CONF_NAME: "Casa"}, unique_id="77")
    entry.add_to_hass(hass)
    avisos: list = []

    async def fake_fetch(self) -> None:
        self.open_meteo_hourly = _hours()
        self.shortterm_alerts = list(avisos)

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.SENSOR]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_short_term_summary")
        state = hass.states.get(entity_id)
        assert state.state == "Hoy: despejado · hasta 26 °C"
        assert state.attributes["titular"] == state.state
        assert state.attributes["pronostico"].startswith("Hoy: despejado, hasta 26 °C, sin lluvia.\nEsta noche:")
        assert [p["titulo"] for p in state.attributes["partes"]] == ["Hoy", "Esta noche", "Mañana jueves"]

        avisos.append({"title": "TORMENTAS FUERTES", "end_date": "2026-10-07T10:00:00-03:00", "zones": []})
        await hass.data[DOMAIN][entry.entry_id].async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.state.startswith("⚠️ Tormentas fuertes")
        assert state.attributes["pronostico"].startswith("⚠️ Tormentas fuertes (vigente hasta las 10:00).\nHoy:")
