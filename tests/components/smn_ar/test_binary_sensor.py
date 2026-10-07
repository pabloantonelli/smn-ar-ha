"""Test the SMN alert binary sensors.

Zone alerts follow SMN's level for the current period of the day; the
storm/rain/wind/snow/zonda/hail/short-term alerts turn on by distance to the
nearest matching aviso a muy corto plazo.
"""
from datetime import datetime
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.smn_ar.const import DOMAIN

# 08:00 in Argentina (UTC-3): the "morning" period.
MORNING = "2026-10-07 11:00:00+00:00"
NIGHT = "2026-10-07 21:00:05+00:00"

# A polygon whose closest edge is ~19.7 km south of the configured location.
_POLYGON_20KM_SOUTH = {
    "type": "Polygon",
    "coordinates": [[[-58.50, -34.80], [-58.35, -34.80], [-58.35, -34.90], [-58.50, -34.90]]],
}


def _aviso(title: str) -> dict:
    return {
        "id": 1,
        "title": title,
        "date": "2026-10-07T07:30:00-03:00",
        "end_date": "2026-10-07T09:30:00-03:00",
        "zones": ["BUENOS AIRES: La Matanza."],
        "severity": "N",
        "geometry": _POLYGON_20KM_SOUTH,
        "region": "Sector Centro",
        "instructions": "Buscá refugio.",
    }


STORM_AVISO = _aviso("TORMENTAS FUERTES CON LLUVIAS INTENSAS Y OCASIONAL CAIDA DE GRANIZO. ")
ZONDA_AVISO = _aviso("VIENTO ZONDA CON RÁFAGAS. ")

ZONE_ALERTS = {
    "area_id": 762,
    "updated": "2026-10-07T06:24:12-03:00",
    "warnings": [
        {
            "date": "2026-10-07",
            "max_level": 4,
            "events": [
                {
                    "id": 40,  # niebla
                    "max_level": 3,
                    "levels": {"early_morning": None, "morning": 3, "afternoon": 2, "night": 1},
                },
                {
                    "id": 41,  # tormenta
                    "max_level": 4,
                    "levels": {"early_morning": None, "morning": 3, "afternoon": 4, "night": 1},
                },
            ],
        }
    ],
    "reports": [
        {"event_id": 40, "levels": [{"level": 3, "description": "Niebla densa", "instruction": "Manejá con cuidado"}]},
    ],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/smn_ar."""
    yield


async def _setup(
    hass: HomeAssistant,
    *,
    alerts: dict | None = None,
    avisos: list | None = None,
    options: dict | None = None,
    before_setup=None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ciudad de Buenos Aires",
        data={CONF_LATITUDE: -34.6217, CONF_LONGITUDE: -58.4258, CONF_NAME: "Ciudad de Buenos Aires"},
        options=options or {},
        unique_id="4864",
    )
    entry.add_to_hass(hass)
    if before_setup:
        before_setup(entry)

    async def fake_fetch(self) -> None:
        self.alerts = alerts or {}
        self.nationwide_shortterm_alerts = avisos or []

    with patch(
        "custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch
    ), patch("custom_components.smn_ar.PLATFORMS", [Platform.BINARY_SENSOR]), patch(
        "homeassistant.loader.Integration.dependencies", []
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state(hass: HomeAssistant, entry: MockConfigEntry, suffix: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}{suffix}"
    )
    assert entity_id is not None, suffix
    return hass.states.get(entity_id)


async def test_zone_alerts_follow_current_period(hass: HomeAssistant, freezer) -> None:
    """Fog (zone alert) and the summary are on in the morning and off at night."""
    freezer.move_to(MORNING)
    entry = await _setup(hass, alerts=ZONE_ALERTS)

    fog = _state(hass, entry, "_alert_niebla")
    assert fog.state == STATE_ON
    assert fog.attributes["level"] == 3
    assert fog.attributes["period"] == "mañana"
    assert fog.attributes["max_level_today"] == 3
    assert fog.attributes["description"] == "Niebla densa"
    assert "franja actual" in fog.attributes["criterio"]

    summary = _state(hass, entry, "_alert")
    assert summary.state == STATE_ON
    assert summary.attributes["active_alert_count"] == 2

    # Storm is proximity-based: the zone forecast is only context.
    storm = _state(hass, entry, "_alert_tormenta")
    assert storm.state == STATE_OFF
    assert storm.attributes["zone_level_now"] == 3
    assert storm.attributes["zone_max_level_today"] == 4

    # Period change (18:00 Argentina) refreshes the state without a coordinator update.
    freezer.move_to(NIGHT)
    async_fire_time_changed(hass, datetime.fromisoformat(NIGHT))
    await hass.async_block_till_done()

    assert _state(hass, entry, "_alert_niebla").state == STATE_OFF
    summary = _state(hass, entry, "_alert")
    assert summary.state == STATE_OFF
    assert len(summary.attributes["today_alerts"]) == 2


async def test_zone_alerts_ignore_other_days(hass: HomeAssistant, freezer) -> None:
    """A forecast dated another day doesn't turn anything on."""
    freezer.move_to("2026-10-08 11:00:00+00:00")
    entry = await _setup(hass, alerts=ZONE_ALERTS)

    assert _state(hass, entry, "_alert_niebla").state == STATE_OFF
    assert _state(hass, entry, "_alert").state == STATE_OFF


async def test_nearby_alerts_within_radius(hass: HomeAssistant, freezer) -> None:
    """A storm+rain+hail aviso ~20 km away is within the default 30 km radius."""
    freezer.move_to(MORNING)
    entry = await _setup(hass, avisos=[STORM_AVISO])

    for suffix in ("_alert_tormenta", "_alert_lluvia", "_hail_alert", "_shortterm_alert"):
        state = _state(hass, entry, suffix)
        assert state.state == STATE_ON, suffix
        assert state.attributes["radius_km"] == 30
        assert state.attributes["distance_km"] == pytest.approx(19.7, abs=0.5)
        assert state.attributes["direction"] == "S"
        assert "30 km" in state.attributes["criterio"]

    for suffix in ("_alert_viento", "_alert_nevada", "_alert_viento_zonda"):
        assert _state(hass, entry, suffix).state == STATE_OFF, suffix

    short_term = _state(hass, entry, "_shortterm_alert")
    assert short_term.attributes["alert_count"] == 1
    assert "TORMENTAS FUERTES" in short_term.attributes["title"]


async def test_nearby_alerts_outside_radius(hass: HomeAssistant, freezer) -> None:
    """With a 10 km radius the same aviso is too far, but its distance is still reported."""
    freezer.move_to(MORNING)
    entry = await _setup(hass, avisos=[STORM_AVISO], options={"alert_radius_km": 10})

    storm = _state(hass, entry, "_alert_tormenta")
    assert storm.state == STATE_OFF
    assert storm.attributes["distance_km"] == pytest.approx(19.7, abs=0.5)
    assert _state(hass, entry, "_hail_alert").state == STATE_OFF
    assert _state(hass, entry, "_shortterm_alert").attributes["alert_count"] == 0


async def test_legacy_hail_radius_option(hass: HomeAssistant, freezer) -> None:
    """The former "Granizo cercano" radius option still applies until changed."""
    freezer.move_to(MORNING)
    entry = await _setup(hass, avisos=[STORM_AVISO], options={"hail_radius_km": 10})

    assert _state(hass, entry, "_hail_alert").attributes["radius_km"] == 10
    assert _state(hass, entry, "_alert_tormenta").state == STATE_OFF


async def test_zonda_is_not_wind(hass: HomeAssistant, freezer) -> None:
    """A zonda aviso turns on "viento zonda" but not "viento"."""
    freezer.move_to(MORNING)
    entry = await _setup(hass, avisos=[ZONDA_AVISO])

    assert _state(hass, entry, "_alert_viento_zonda").state == STATE_ON
    assert _state(hass, entry, "_alert_viento").state == STATE_OFF


async def test_nearby_hail_entity_removed(hass: HomeAssistant, freezer) -> None:
    """The merged "Granizo cercano" entity is dropped from the registry."""
    freezer.move_to(MORNING)
    registry = er.async_get(hass)

    def register_legacy_entity(entry: MockConfigEntry) -> None:
        registry.async_get_or_create(
            "binary_sensor", DOMAIN, f"{entry.entry_id}_nearby_hail", config_entry=entry
        )

    entry = await _setup(hass, before_setup=register_legacy_entity)

    assert registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_nearby_hail") is None
