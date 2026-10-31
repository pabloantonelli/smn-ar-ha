"""Test the SMN config flow."""
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smn_ar.const import CONF_PROXY_URL, DOMAIN

@pytest.fixture(autouse=True)
def _no_dependencies():
    """Loading the flow would otherwise set up `frontend`, which isn't installed for tests."""
    with patch("homeassistant.loader.Integration.dependencies", []):
        yield


_INPUT = {CONF_LATITUDE: -31.4201, CONF_LONGITUDE: -64.1888, CONF_NAME: "Córdoba"}


async def _start(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_form_creates_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    result = await _start(hass)
    with patch("custom_components.smn_ar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**_INPUT, CONF_PROXY_URL: "http://proxy:6942/"}
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Córdoba"
    assert result["data"] == {**_INPUT, CONF_PROXY_URL: "http://proxy:6942"}  # no trailing slash


async def test_form_defaults_to_the_home_location(hass: HomeAssistant, enable_custom_integrations) -> None:
    hass.config.latitude = -34.6217
    hass.config.longitude = -58.4258
    result = await _start(hass)
    defaults = {str(key): key.default() for key in result["data_schema"].schema if callable(key.default)}
    assert defaults[CONF_LATITUDE] == -34.6217
    assert defaults[CONF_LONGITUDE] == -58.4258


async def test_form_already_configured(hass: HomeAssistant, enable_custom_integrations) -> None:
    MockConfigEntry(domain=DOMAIN, data=_INPUT, unique_id="cordoba").add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_INPUT, CONF_NAME: "Otro nombre", CONF_PROXY_URL: "http://proxy:6942"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "already_configured"}
