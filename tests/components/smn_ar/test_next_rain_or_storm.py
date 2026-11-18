"""Test "Próxima lluvia o tormenta" (find_next_rain_or_storm)."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.smn_ar.sensor import (
    daily_rain_odds,
    find_next_rain_or_storm,
    next_rain_message,
    rain_curve,
    storm_odds,
)

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
    assert next_any["smn_probability"] == 70
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
    assert next_any["smn_probability"] == 70


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
    assert state.attributes["probabilidad"] is None  # no Open-Meteo to measure it by
    assert "no una certeza" in state.attributes["criterio"]
    assert state.attributes["titulo"] == "Lluvia posible desde las 12:00"
    assert state.attributes["curva"] == []
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
    assert next_any["smn_probability"] == 70  # still the SMN period's
    assert next_any["odds"] == 11  # one 30% hour among dry ones, from 13:00 to 16:00


def test_smn_period_and_alert_need_open_meteo_rain() -> None:
    # Open-Meteo dry all day: neither the rainy SMN period nor the zone alert counts.
    open_meteo_hourly = [_hour(h, 10, 0.0) for h in range(6, 24)]
    alerts = _alerts(41, {"early_morning": None, "morning": 3, "afternoon": 3, "night": 1})
    assert _find_with([_period("12:00", 89, [70, 100])], alerts, open_meteo_hourly=open_meteo_hourly) == (None, None)
    # A probable hour with hardly any rain doesn't confirm it either.
    open_meteo_hourly[8] = _hour(14, 50, 0.0)
    assert _find_with([_period("12:00", 73, [40, 70])], open_meteo_hourly=open_meteo_hourly) == (None, None)


def test_hourly_forecast_alone() -> None:
    open_meteo_hourly = [_hour(9, 10, 0.0), _hour(10, 60, 2.0), _hour(11, 50, 1.0, code=95)]
    next_any, next_storm = _find_with(open_meteo_hourly=open_meteo_hourly)
    assert next_any["source"][1] == "pronóstico horario"
    assert next_any["when"] == datetime(2026, 10, 7, 10, 0, tzinfo=ARG)
    assert [pct for _, pct in next_any["curve"]] == [10, 27, 34]  # Open-Meteo's hours start at 9:00
    assert next_any["odds"] == 34
    # The weather code alone no longer makes a storm (see storm_odds).
    assert next_storm is None


def _stormy(hour: int, probability: int = 90, precipitation: float = 3.0, cape: float = 2000, aifs: float = 2.0, ukmo: float = 1.0) -> dict:
    return {**_hour(hour, probability, precipitation), "cape": cape, "precipitation_aifs": aifs, "precipitation_ukmo": ukmo}


def _calm(hour: int) -> dict:
    return _stormy(hour, 5, 0.0, cape=50, aifs=0.0, ukmo=0.0)


def test_storm_odds_from_the_models() -> None:
    hours = [_calm(h) for h in range(8, 12)] + [_stormy(h) for h in range(12, 16)]
    start = datetime(2026, 10, 7, 12, 0, tzinfo=ARG)
    assert storm_odds(hours, start) >= 50
    assert storm_odds(hours, datetime(2026, 10, 7, 8, 0, tzinfo=ARG)) <= 2
    # Without the AIFS/UKMO rain it falls back to best_match alone, lower here.
    no_models = [{**h, "precipitation_aifs": None} for h in hours]
    assert 0 < storm_odds(no_models, start) < storm_odds(hours, start)
    # Not all 3 hours there: unknown.
    assert storm_odds(hours, datetime(2026, 10, 7, 14, 0, tzinfo=ARG)) is None


def test_storm_starts_with_the_rain() -> None:
    # 10:00 and 11:00 are dry, though the storm is within their 3 hours.
    hours = [_calm(h) for h in range(8, 12)] + [_stormy(h) for h in range(12, 16)]
    next_any, next_storm = _find_with(open_meteo_hourly=hours)
    assert next_any is next_storm
    assert next_any["tipo"] == "tormenta"
    assert next_any["when"] == datetime(2026, 10, 7, 12, 0, tzinfo=ARG)
    assert next_any["storm_odds"] >= 50


def test_rain_without_storm_signal_stays_rain() -> None:
    hours = [_calm(h) for h in range(8, 12)] + [_stormy(h, 70, 1.0, cape=0, aifs=0.2, ukmo=0.0) for h in range(12, 16)]
    next_any, next_storm = _find_with(open_meteo_hourly=hours)
    assert next_any["tipo"] == "lluvia"
    assert next_any["storm_odds"] < 25
    assert next_storm is None


def test_curve_adds_the_models_rain() -> None:
    start = datetime(2026, 10, 7, 10, 0, tzinfo=ARG)
    plain = [_hour(h, 50, 0.5) for h in range(10, 16)]
    wet = [{**h, "precipitation_aifs": 2.0, "precipitation_ukmo": 1.0} for h in plain]
    dry = [{**h, "precipitation_aifs": 0.0, "precipitation_ukmo": 0.0} for h in plain]
    wet_curve, dry_curve, plain_curve = (rain_curve(hours, start) for hours in (wet, dry, plain))
    assert wet_curve[-1][1] > plain_curve[-1][1] > dry_curve[-1][1]
    # Without the models, the curve is the one from before.
    assert [pct for _, pct in plain_curve] == [pct for _, pct in rain_curve([_hour(h, 50, 0.5) for h in range(10, 16)], start)]


def test_daily_rain_odds() -> None:
    utc = timezone.utc
    first = datetime(2026, 10, 7, 0, 0, tzinfo=utc)
    hours = []
    for index in range(5 * 24):
        when = first + timedelta(hours=index)
        day = (when - timedelta(hours=1)).astimezone(ARG).date().day
        hours.append({"when": when, "precipitation_aifs": {8: 0.0, 9: 0.25}.get(day, 0.0), "precipitation_ukmo": 0.0})
    days = daily_rain_odds(hours, NOW)
    assert [d["fecha"] for d in days] == ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert days[0] == {"fecha": "2026-10-08", "probabilidad": 9, "mm": 0.0}
    assert days[1]["mm"] == 6.0 and days[1]["probabilidad"] >= 85
    # A day the models don't fully cover is left out.
    assert len(daily_rain_odds(hours[:60], NOW)) == 1


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
    assert next_any["odds"] is None  # a projected arrival: not measured


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


def test_forecasts_count_within_a_dry_radar_horizon() -> None:
    from custom_components.smn_ar.nowcast import SOURCE_RADAR, Nowcast

    # Drizzle and light rain are under the radar's threshold.
    horizon_end = NOW + timedelta(minutes=100)
    dry_radar = Nowcast(source=SOURCE_RADAR, frame_time=NOW - timedelta(minutes=20), horizon_end=horizon_end, motion=None)
    open_meteo_hourly = [_hour(8, 80, 3.0, code=95)]
    next_any, _ = _find_with(open_meteo_hourly=open_meteo_hourly, nowcast=dry_radar)
    assert next_any["when"] == datetime(2026, 10, 7, 8, 0, tzinfo=ARG)
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
    assert _find_with(nowcast=nowcast(30))[0]["odds"] == 80  # cold tops over the location
    assert _find_with(nowcast=nowcast(180)) == (None, None)  # the source kept failing
    assert _find_with(nowcast=nowcast(30, until=-5)) == (None, None)  # already went past


NOW_CORDOBA = datetime(2026, 10, 7, 12, 38, tzinfo=ARG)
HOURS_CORDOBA = [(12, 0, 0.0), (13, 26, 0.1), (14, 62, 0.6), (15, 86, 0.5), (16, 88, 0.3), (17, 79, 0.2)]
AFTERNOON_ALERT = {"early_morning": None, "morning": 1, "afternoon": 3, "night": 1}


def _find_cordoba():
    return find_next_rain_or_storm(
        [_period("12:00", 73, [40, 70])], _alerts(41, AFTERNOON_ALERT), None, LAT, LON, 30, NOW_CORDOBA,
        open_meteo_hourly=[_hour(h, p, mm) for h, p, mm in HOURS_CORDOBA],
    )


def test_curve_gives_the_odds_their_window() -> None:
    next_any, _ = _find_cordoba()
    assert next_any["source"][1] == "alerta de zona"
    assert next_any["when"] == datetime(2026, 10, 7, 14, 0, tzinfo=ARG)
    assert [pct for _, pct in next_any["curve"]] == [1, 11, 25, 39, 51, 57]
    assert next_any["odds"] == 57
    assert next_any["odds_until"] == datetime(2026, 10, 7, 18, 0, tzinfo=ARG)
    assert next_any["hour_odds"] == 29
    assert next_any["hourly_probability"] == 62
    assert next_any["smn_probability"] == 70


def test_curve_is_never_lower_for_a_longer_window() -> None:
    # A dry hour after a wet one: 1 h gives more than the uncorrected 2 h would.
    curve = rain_curve([_hour(10, 90), _hour(11, 0, 0.0)], datetime(2026, 10, 7, 10, 0, tzinfo=ARG))
    assert len(curve) == 2
    assert curve[1][1] >= curve[0][1]


def test_curve_starts_shortly_before_a_later_rain() -> None:
    hours = [_hour(h, 0, 0.0) for h in range(8, 20)] + [_hour(20, 90)]
    next_any, _ = _find_with(open_meteo_hourly=hours)
    assert next_any["curve"][0][0] == datetime(2026, 10, 7, 19, 0, tzinfo=ARG)  # from 18:00


def test_message_for_a_forecast() -> None:
    next_any, next_storm = _find_cordoba()
    assert next_rain_message(next_any, next_storm, NOW_CORDOBA) == {
        "titulo": "Tormenta posible desde las 14:00",
        # The zone alert says storm; the models give it this chance.
        "detalle": "25% antes de las 15:00 · 57% antes de las 18:00 · alerta amarilla · chance de tormenta 2% en 3 h",
        "icono": "mdi:weather-cloudy-clock",
        "color": "orange",
    }


def test_message_with_nothing_ahead() -> None:
    assert next_rain_message(None, None, NOW)["titulo"] == "Sin lluvia a la vista"


def test_message_never_says_a_forecast_is_under_way() -> None:
    next_any, _ = _find_with(open_meteo_hourly=[_hour(8, 60, 1.0)])
    assert next_any["when"] <= NOW
    message = next_rain_message(next_any, None, NOW)
    assert message["titulo"] == "Lluvia posible ahora"
    assert message["icono"] == "mdi:weather-cloudy-clock"


def test_message_for_an_aviso_inside() -> None:
    next_any = {
        "when": NOW - timedelta(minutes=10), "tipo": "tormenta", "source": (4, "aviso cercano"), "distance_km": 0,
        "storm_odds": 3,
    }
    message = next_rain_message(next_any, next_any, NOW)
    assert message["titulo"] == "Aviso de tormenta vigente"
    assert message["detalle"] == "según aviso cercano"  # seen, not forecast: no chance shown
    assert message["color"] == "red"


def test_message_for_an_approaching_echo_and_a_later_storm() -> None:
    storm = {"when": NOW + timedelta(days=1), "tipo": "tormenta", "source": (3, "alerta de zona")}
    next_any = {
        "when": NOW + timedelta(minutes=40), "tipo": "lluvia", "source": (5, "radar SINARAME Córdoba"),
        "distance_km": 12.3, "direction": "W", "odds": 42, "odds_until": NOW + timedelta(hours=2),
    }
    message = next_rain_message(next_any, storm, NOW)
    assert message["titulo"] == "Lluvia cerca de las 08:40"
    assert message["detalle"] == (
        "42% antes de las 10:00 · a 12 km al oeste · según radar SINARAME Córdoba"
        " · tormenta posible desde mañana a las 08:00"
    )


async def test_storm_and_rain_tomorrow_entities(hass, enable_custom_integrations, freezer) -> None:
    """Probabilidad de tormenta and Lluvia mañana, from the Open-Meteo hours."""
    from unittest.mock import patch

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
    from homeassistant.helpers import entity_registry as er

    from custom_components.smn_ar.const import DOMAIN

    freezer.move_to("2026-10-07 15:20:00+00:00")  # 12:20 in Argentina
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_LATITUDE: LAT, CONF_LONGITUDE: LON, CONF_NAME: "Casa"}, unique_id="4865"
    )
    entry.add_to_hass(hass)
    utc = timezone.utc
    first = datetime(2026, 10, 7, 0, 0, tzinfo=utc)
    models = [
        {"when": first + timedelta(hours=i), "precipitation_aifs": 0.25, "precipitation_ukmo": 0.0} for i in range(120)
    ]

    async def fake_fetch(self) -> None:
        self.open_meteo_hourly = [
            {**(_stormy(0) if 15 <= h < 18 else _calm(0)), "when": first + timedelta(hours=h)} for h in range(72)
        ]
        self.open_meteo_models = models
        self.hourly_forecast = [_period("12:00", 73, [40, 70], date="2026-10-08")]

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.SENSOR]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = er.async_get(hass)
    storm = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_storm_odds"))
    assert int(storm.state) >= 50  # 15-18 UTC is stormy
    assert storm.attributes["unit_of_measurement"] == "%"
    assert storm.attributes["fuente"] == "Open-Meteo: best_match, ECMWF AIFS y UK Met Office"
    assert len(storm.attributes["proximas_horas"]) == 8
    assert storm.attributes["proximas_horas"][0]["desde"] == "2026-10-07T12:00:00-03:00"
    assert storm.attributes["maxima"] == int(storm.state)

    rain = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_rain_tomorrow"))
    assert int(rain.state) >= 85  # 6 mm all day
    assert rain.attributes["dias"][0] == {
        "fecha": "2026-10-08", "probabilidad": int(rain.state), "mm": 6.0, "probabilidad_smn": 70,
    }
    assert rain.attributes["dias"][1]["probabilidad_smn"] is None


def test_smn_storm_shows_the_models_storm_chance() -> None:
    """SMN says storm for the afternoon; the models hardly see one."""
    hours = [_calm(h) for h in range(8, 12)] + [_stormy(h, 40, 0.5, cape=100, aifs=0.1, ukmo=0.0) for h in range(12, 18)]
    next_any, next_storm = _find_with([_period("12:00", 76, [10, 20])], open_meteo_hourly=hours)
    assert next_any["tipo"] == "tormenta"
    assert next_any["source"][1] == "pronóstico"
    assert next_any["when"] == datetime(2026, 10, 7, 12, 0, tzinfo=ARG)
    odds = next_any["storm_odds"]
    assert odds is not None and odds < 25
    detalle = next_rain_message(next_any, next_storm, NOW)["detalle"]
    assert detalle.endswith(f"según el SMN · chance de tormenta {odds}% en 3 h")


def test_summary_says_the_condition_in_spanish() -> None:
    from custom_components.smn_ar.sensor import build_summary

    state, _ = build_summary([], {}, {}, {}, [{"weather": {"id": 76}, "temp_max": 26, "temp_min": 19}])
    assert state == "Sin alertas vigentes. Hoy: tormenta, máxima 26°C / mínima 19°C"
