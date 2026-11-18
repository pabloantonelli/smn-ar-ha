"""The Resumen's forecast text (narrative.py)."""
from datetime import datetime, timedelta, timezone

from custom_components.smn_ar.narrative import build_forecast

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


def test_morning_tells_the_rest_of_the_day_and_tomorrow() -> None:
    headline, text = build_forecast(_at(0, 8), _hours(), [], [], [{"fecha": "2026-10-08", "probabilidad": 9}])
    assert headline == "Sin lluvia el resto del día · 16 a 26 °C"
    assert text == (
        "Esta mañana, despejada, entre 16 y 21 °C. "
        "A la tarde, despejada, con máxima de 26 °C a las 16. "
        "A la noche, despejada, con mínima de 18 °C. "
        "Sin lluvia en lo que queda del día. "
        "Mañana jueves: sin lluvia (9 %), de 14 a 26 °C."
    )


def test_afternoon_storms_lead_the_headline() -> None:
    stormy = [_at(0, h) for h in range(15, 19)]
    headline, text = build_forecast(_at(0, 9), _hours(stormy=stormy), [], [], [])
    assert headline.startswith("A la tarde con posibles tormentas")
    assert "Lluvia casi segura desde las 15" in text
    assert "tormentas probables" in text or "posibles tormentas" in text


def test_smn_storm_the_models_dont_see() -> None:
    smn = [{"date": "2026-10-07", "time": "12:00", "weather": {"id": 76}}]
    _, text = build_forecast(_at(0, 9), _hours(), smn, [], [])
    assert "A la tarde, despejada, con máxima de 26 °C a las 16. El SMN anuncia tormentas, aunque por ahora se ven poco probables (0 %)." in text


def test_evening_tells_tomorrow_by_periods() -> None:
    rainy = [_at(1, h) for h in range(13, 18)]
    headline, text = build_forecast(
        _at(0, 20), _hours(rainy=rainy), [], [{"date": "2026-10-08", "weather": {"id": 76}}],
        [{"fecha": "2026-10-08", "probabilidad": 90}],
    )
    assert headline.startswith("Mañana a la tarde con lluvia")
    assert text.startswith("Esta noche,")
    assert "Mañana jueves: a la mañana," in text
    assert "A la tarde, nublada," in text
    assert text.endswith("En todo el día, lluvia casi segura (90 %), de 14 a 26 °C; el SMN pronostica tormenta para mañana.")


def test_strong_wind_is_mentioned() -> None:
    _, text = build_forecast(_at(0, 13), _hours(windy=[_at(0, 15)]), [], [], [])
    assert "Viento del noreste de 35 km/h con ráfagas de 60." in text


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
        assert state.state == "Sin lluvia el resto del día · 16 a 26 °C"
        assert state.attributes["titular"] == state.state
        assert state.attributes["pronostico"].startswith("Esta mañana, despejada")

        avisos.append({"title": "TORMENTAS FUERTES", "end_date": "2026-10-07T10:00:00-03:00", "zones": []})
        await hass.data[DOMAIN][entry.entry_id].async_refresh()
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert state.state.startswith("⚠️ Tormentas fuertes")
        assert state.attributes["pronostico"].startswith("⚠️ Tormentas fuertes (vigente hasta las 10:00). Esta mañana")
