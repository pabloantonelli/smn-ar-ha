"""Test the SMN weather entity."""
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.weather import ATTR_CONDITION_CLEAR_NIGHT, ATTR_CONDITION_SUNNY
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
from homeassistant.core import HomeAssistant

from custom_components.smn_ar.const import DOMAIN

ENTITY_ID = "weather.ciudad_de_buenos_aires"
CURRENT = {
    "temperature": 22.5,
    "humidity": 65,
    "pressure": 1013.2,
    "wind_speed": 15.5,
    "wind_deg": 180,
    "weather": {"id": 3, "description": "Despejado"},
    "name": "Ciudad de Buenos Aires",
}


async def _setup(hass: HomeAssistant, daily: list | None = None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ciudad de Buenos Aires",
        data={CONF_LATITUDE: -34.6217, CONF_LONGITUDE: -58.4258, CONF_NAME: "Ciudad de Buenos Aires"},
        unique_id="4864",
    )
    entry.add_to_hass(hass)

    async def fake_fetch(self) -> None:
        self.current_weather_data = CURRENT
        self.daily_forecast = daily or []

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.WEATHER]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_weather_entity_state(hass: HomeAssistant, enable_custom_integrations) -> None:
    await _setup(hass)
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state in (ATTR_CONDITION_SUNNY, ATTR_CONDITION_CLEAR_NIGHT)
    assert state.attributes["temperature"] == 22.5
    assert state.attributes["humidity"] == 65
    assert state.attributes["pressure"] == 1013.2
    assert state.attributes["wind_speed"] == 15.5
    assert state.attributes["wind_bearing"] == 180
    assert "Servicio Meteorológico Nacional" in state.attributes["attribution"]


async def test_weather_entity_daily_forecast(hass: HomeAssistant, enable_custom_integrations, mock_forecast_data) -> None:
    await _setup(hass, mock_forecast_data["forecast"])
    response = await hass.services.async_call(
        "weather", "get_forecasts", {"entity_id": ENTITY_ID, "type": "daily"}, blocking=True, return_response=True
    )
    assert response[ENTITY_ID]["forecast"]


async def test_weather_entity_name(hass: HomeAssistant, enable_custom_integrations) -> None:
    await _setup(hass)
    assert hass.states.get(ENTITY_ID).attributes["friendly_name"] == "Ciudad de Buenos Aires"


@pytest.mark.asyncio
async def test_weather_condition_mapping(
    hass: HomeAssistant,
) -> None:
    """Test weather condition ID mapping."""
    from custom_components.smn_ar.weather import format_condition

    # Test day conditions
    assert format_condition({"id": 3, "description": "Despejado"}, sun_is_up=True) == ATTR_CONDITION_SUNNY
    assert format_condition({"id": 5, "description": "Despejado"}, sun_is_up=False) == ATTR_CONDITION_CLEAR_NIGHT

    # Test sunny converted to clear-night at night
    assert format_condition({"id": 3, "description": "Despejado"}, sun_is_up=False) == ATTR_CONDITION_CLEAR_NIGHT

    # Test None handling
    assert format_condition(None, sun_is_up=True) == ATTR_CONDITION_SUNNY
    assert format_condition(None, sun_is_up=False) == ATTR_CONDITION_CLEAR_NIGHT

    # Test invalid dict
    assert format_condition({}, sun_is_up=True) == ATTR_CONDITION_SUNNY
    assert format_condition({"description": "test"}, sun_is_up=True) == ATTR_CONDITION_SUNNY
