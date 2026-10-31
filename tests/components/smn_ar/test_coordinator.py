"""Test the SMN data refresh cadence."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.smn_ar.coordinator import (
    ArgentinaSMNData,
    ArgentinaSMNDataUpdateCoordinator,
)


def _http_error(status: int) -> aiohttp.ClientResponseError:
    return aiohttp.ClientResponseError(MagicMock(), (), status=status)


async def test_shortterm_every_poll_rest_every_30_min() -> None:
    """Avisos are fetched on every 10-min poll; the rest only every 30 min."""
    data = ArgentinaSMNData.__new__(ArgentinaSMNData)
    data._location_id = "10823"
    data._last_full_fetch = None
    for name in (
        "_fetch_current_weather",
        "_fetch_forecast",
        "_fetch_sun",
        "_fetch_alerts",
        "_fetch_open_meteo_hourly",
        "_fetch_shortterm_alerts",
        "_fetch_nationwide_shortterm_alerts",
    ):
        setattr(data, name, AsyncMock())

    clock = MagicMock()
    with patch("custom_components.smn_ar.coordinator.time.monotonic", clock):
        for minute in (0, 10, 20, 29.99, 40):
            clock.return_value = minute * 60
            await data.fetch_data()

    assert data._fetch_nationwide_shortterm_alerts.await_count == 5
    assert data._fetch_shortterm_alerts.await_count == 5
    # Minute 0 and minute ~30 (slightly early still counts).
    assert data._fetch_forecast.await_count == 2
    assert data._fetch_alerts.await_count == 2
    assert data._fetch_open_meteo_hourly.await_count == 2


AVISO = {
    "title": "TORMENTAS FUERTES",
    "date": "2026-10-31T15:00:00-03:00",
    "end_date": "2099-01-01T16:00:00-03:00",
}
EXPIRED_AVISO = {**AVISO, "title": "YA VENCIDO", "end_date": "2020-01-01T16:00:00-03:00"}


def _data_with(alerts: list, get_json: AsyncMock) -> ArgentinaSMNData:
    data = ArgentinaSMNData.__new__(ArgentinaSMNData)
    data.shortterm_alerts = list(alerts)
    data._get_json = get_json
    return data


@pytest.mark.parametrize(
    "failure",
    [aiohttp.ClientConnectionError(), asyncio.TimeoutError(), _http_error(503)],
)
async def test_failed_fetch_keeps_known_avisos(failure: Exception) -> None:
    """A failed poll says nothing about the avisos: they stay."""
    data = _data_with([AVISO], AsyncMock(side_effect=failure))
    await data._fetch_shortterm_alerts("10823")
    assert data.shortterm_alerts == [AVISO]


async def test_failed_fetch_drops_expired_avisos() -> None:
    """An outage can't keep an aviso alive past its end date."""
    data = _data_with(
        [AVISO, EXPIRED_AVISO], AsyncMock(side_effect=aiohttp.ClientConnectionError())
    )
    await data._fetch_shortterm_alerts("10823")
    assert data.shortterm_alerts == [AVISO]


async def test_unexpected_payload_keeps_known_avisos() -> None:
    """A body that isn't a list isn't an answer either."""
    data = _data_with([AVISO], AsyncMock(return_value={"error": "session expired"}))
    await data._fetch_shortterm_alerts("10823")
    assert data.shortterm_alerts == [AVISO]


@pytest.mark.parametrize(
    "get_json",
    [AsyncMock(return_value=[]), AsyncMock(side_effect=_http_error(404))],
)
async def test_empty_answer_clears_avisos(get_json: AsyncMock) -> None:
    """An empty list, or a 404 for "none here", does lift the avisos."""
    data = _data_with([AVISO], get_json)
    await data._fetch_shortterm_alerts("10823")
    assert data.shortterm_alerts == []


async def test_outage_does_not_refire_event() -> None:
    """Failing and recovering fires nothing: the aviso never went away."""
    data = _data_with([AVISO], AsyncMock())
    coordinator = ArgentinaSMNDataUpdateCoordinator.__new__(
        ArgentinaSMNDataUpdateCoordinator
    )
    coordinator.hass = MagicMock()
    coordinator._smn_data = data
    coordinator._config_entry = MagicMock(entry_id="entry")
    coordinator._previous_shortterm_signatures = None
    coordinator._fire_shortterm_alert_event_if_changed()

    data._get_json = AsyncMock(side_effect=aiohttp.ClientConnectionError())
    await data._fetch_shortterm_alerts("10823")
    coordinator._fire_shortterm_alert_event_if_changed()

    data._get_json = AsyncMock(return_value=[AVISO])
    await data._fetch_shortterm_alerts("10823")
    coordinator._fire_shortterm_alert_event_if_changed()

    coordinator.hass.bus.async_fire.assert_not_called()
