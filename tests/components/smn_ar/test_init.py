"""Test the SMN integration's setup, unload and services."""
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, Platform
from homeassistant.core import HomeAssistant

from custom_components.smn_ar.const import API_ALERT_PATH, DOMAIN


async def _setup(hass: HomeAssistant, alerts: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ciudad de Buenos Aires",
        data={CONF_LATITUDE: -34.6217, CONF_LONGITUDE: -58.4258, CONF_NAME: "Ciudad de Buenos Aires"},
        unique_id="4864",
    )
    entry.add_to_hass(hass)

    async def fake_fetch(self) -> None:
        self.alerts = alerts or {}

    with patch("custom_components.smn_ar.coordinator.ArgentinaSMNData.fetch_data", fake_fetch), patch(
        "custom_components.smn_ar.PLATFORMS", [Platform.SENSOR]
    ), patch("homeassistant.loader.Integration.dependencies", []):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_setup_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]


async def test_unload_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = await _setup(hass)
    with patch("custom_components.smn_ar.PLATFORMS", [Platform.SENSOR]):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_service_get_alerts(hass: HomeAssistant, enable_custom_integrations, mock_alerts_data_with_active) -> None:
    entry = await _setup(hass, mock_alerts_data_with_active)
    response = await hass.services.async_call(
        DOMAIN, "get_alerts", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    assert response["area_id"] == mock_alerts_data_with_active["area_id"]
    assert response["active_alerts"]
    assert {"event_name", "level_name", "severity"} <= response["active_alerts"][0].keys()


async def test_service_get_alerts_unknown_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    await _setup(hass)
    response = await hass.services.async_call(
        DOMAIN, "get_alerts", {"config_entry_id": "nope"}, blocking=True, return_response=True
    )
    assert response == {"active_alerts": [], "max_severity": "info", "area_id": None}


async def test_service_get_alerts_for_location(
    hass: HomeAssistant, enable_custom_integrations, mock_alerts_data_with_active
) -> None:
    await _setup(hass)
    with patch(
        "custom_components.smn_ar.coordinator.ArgentinaSMNData._get_json",
        AsyncMock(return_value=mock_alerts_data_with_active),
    ) as get_json:
        response = await hass.services.async_call(
            DOMAIN, "get_alerts_for_location", {"location_id": "4864"}, blocking=True, return_response=True
        )
    get_json.assert_awaited_once_with(f"{API_ALERT_PATH}/4864")
    assert response["active_alerts"]


async def test_service_get_alerts_for_location_reports_errors(
    hass: HomeAssistant, enable_custom_integrations
) -> None:
    await _setup(hass)
    with patch(
        "custom_components.smn_ar.coordinator.ArgentinaSMNData._get_json",
        AsyncMock(side_effect=OSError("proxy down")),
    ):
        response = await hass.services.async_call(
            DOMAIN, "get_alerts_for_location", {"location_id": "4864"}, blocking=True, return_response=True
        )
    assert response["active_alerts"] == []
    assert response["error"] == "proxy down"
