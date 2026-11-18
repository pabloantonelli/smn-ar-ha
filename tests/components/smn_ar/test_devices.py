"""Each location's entities are grouped in five devices (devices.py)."""
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.smn_ar.const import DOMAIN


async def _setup(hass: HomeAssistant, language: str, before=None) -> MockConfigEntry:
    hass.config.language = language
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_LATITUDE: -31.48, CONF_LONGITUDE: -64.27, CONF_NAME: "Córdoba"}, unique_id="5001"
    )
    entry.add_to_hass(hass)
    if before:
        before(entry)

    async def fake_fetch(self) -> None:
        pass

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.WEATHER]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _device_of(hass: HomeAssistant, entry: MockConfigEntry, domain: str, suffix: str) -> dr.DeviceEntry:
    entity = er.async_get(hass).async_get(
        er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}{suffix}")
    )
    return dr.async_get(hass).async_get(entity.device_id)


@pytest.mark.parametrize(
    ("domain", "suffix", "device"),
    [
        ("weather", "", "Córdoba · Clima actual"),
        ("sensor", "_current_temperature", "Córdoba · Clima actual"),
        ("sensor", "_next_rain", "Córdoba · Pronóstico"),
        ("sensor", "_storm_odds", "Córdoba · Pronóstico"),
        ("sensor", "_tomorrow_forecast", "Córdoba · Pronóstico"),
        ("sensor", "_short_term_summary", "Córdoba · Pronóstico"),
        ("sensor", "_nationwide_avisos", "Córdoba · Avisos cercanos"),
        ("binary_sensor", "_shortterm_alert", "Córdoba · Avisos cercanos"),
        ("binary_sensor", "_alert_tormenta", "Córdoba · Avisos cercanos"),
        ("binary_sensor", "_hail_alert", "Córdoba · Avisos cercanos"),
        ("binary_sensor", "_alert", "Córdoba · Alertas de la zona"),
        ("binary_sensor", "_alert_niebla", "Córdoba · Alertas de la zona"),
    ],
)
async def test_entities_land_in_their_device(hass, enable_custom_integrations, domain, suffix, device) -> None:
    entry = await _setup(hass, "es")
    assert _device_of(hass, entry, domain, suffix).name == device


async def test_device_names_follow_the_language(hass, enable_custom_integrations) -> None:
    entry = await _setup(hass, "en")
    assert _device_of(hass, entry, "sensor", "_storm_odds").name == "Córdoba · Forecast"
    assert _device_of(hass, entry, "binary_sensor", "_alert_niebla").name == "Córdoba · Zone alerts"


async def test_upgrade_keeps_entity_ids_and_the_device(hass, enable_custom_integrations) -> None:
    """An install from before: one device, the old entity_ids. Entities move, ids stay."""
    old = {}

    def before(entry: MockConfigEntry) -> None:
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}, name="Córdoba"
        )
        registry = er.async_get(hass)
        for domain, suffix, object_id in (
            ("sensor", "_next_rain", "cordoba_proxima_lluvia"),
            ("binary_sensor", "_alert_tormenta", "cordoba_alerta_por_tormenta"),
        ):
            old[suffix] = registry.async_get_or_create(
                domain, DOMAIN, f"{entry.entry_id}{suffix}", config_entry=entry,
                device_id=device.id, suggested_object_id=object_id,
            ).entity_id
        old["device"] = device.id

    entry = await _setup(hass, "es", before)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_next_rain") == "sensor.cordoba_proxima_lluvia"
    assert old["_alert_tormenta"] == "binary_sensor.cordoba_alerta_por_tormenta"
    assert _device_of(hass, entry, "binary_sensor", "_alert_tormenta").name == "Córdoba · Avisos cercanos"
    # The original device stays, as the current weather's, not empty.
    original = dr.async_get(hass).async_get(old["device"])
    assert original.name == "Córdoba · Clima actual"
    assert er.async_entries_for_device(registry, original.id)
    # The others hang from it.
    forecast = _device_of(hass, entry, "sensor", "_next_rain")
    assert forecast.via_device_id == original.id
