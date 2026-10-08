"""Test the SMN data refresh cadence."""
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.smn_ar.coordinator import ArgentinaSMNData


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
