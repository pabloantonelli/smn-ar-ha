"""DataUpdateCoordinator for the SMN integration."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_ALERT_PATH,
    API_COLD_WARNING_PATH,
    API_COORD_PATH,
    API_FORECAST_PATH,
    API_HEAT_WARNING_PATH,
    API_SHORTTERM_ALERT_PATH,
    API_SUN_PATH,
    API_WEATHER_PATH,
    CONF_LOCATION_ID,
    CONF_PROXY_URL,
    DEFAULT_PROXY_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ArgentinaSMNData:
    """Fetches and holds SMN data, via the local smn-proxy add-on.

    The add-on (see addons/smn_proxy) is responsible for solving SMN's
    Cloudflare challenge and attaching a valid JWT to every upstream
    request. This client only ever talks to the proxy's base URL, never
    directly to ws1.smn.gob.ar.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        proxy_url: str,
        latitude: float,
        longitude: float,
        location_id: str | None = None,
    ) -> None:
        """Initialize the data object."""
        self._hass = hass
        self._proxy_url = proxy_url.rstrip("/")
        self._latitude = latitude
        self._longitude = longitude
        self._location_id = location_id
        self._session = async_get_clientsession(hass)

        self.current_weather_data: dict[str, Any] = {}
        self.daily_forecast: list[dict[str, Any]] = []
        self.hourly_forecast: list[dict[str, Any]] = []
        self.alerts: dict[str, Any] = {}
        self.shortterm_alerts: list[dict[str, Any]] = []
        self.heat_warnings: dict[str, Any] = {}
        self.cold_warnings: dict[str, Any] = {}
        self.sun: dict[str, Any] = {}

    @property
    def location_id(self) -> str | None:
        """Return the resolved location id, if any."""
        return self._location_id

    def _url(self, path: str) -> str:
        return f"{self._proxy_url}{path}"

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON path from the proxy."""
        url = self._url(path)
        async with async_timeout.timeout(10):
            response = await self._session.get(url, params=params)
            response.raise_for_status()
            return await response.json()

    async def _get_location_id(self) -> str:
        """Resolve and cache the SMN location id for the configured coordinates."""
        if self._location_id:
            return self._location_id

        try:
            data = await self._get_json(
                API_COORD_PATH,
                params={"lat": self._latitude, "lon": self._longitude},
            )
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error fetching location id from proxy: {err}") from err

        if isinstance(data, dict):
            location_id = data.get("id") or data.get("location_id")
        elif isinstance(data, list) and data:
            location_id = data[0].get("id") or data[0].get("location_id")
        else:
            location_id = None

        if not location_id:
            raise UpdateFailed(f"Unexpected location response format: {data}")

        self._location_id = str(location_id)
        _LOGGER.info(
            "Resolved location id for %s,%s: %s",
            self._latitude,
            self._longitude,
            self._location_id,
        )
        return self._location_id

    async def fetch_data(self) -> None:
        """Fetch all SMN data through the proxy."""
        try:
            location_id = await self._get_location_id()
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error resolving location: {err}") from err

        await self._fetch_current_weather(location_id)
        await self._fetch_forecast(location_id)
        await self._fetch_sun(location_id)
        await self._fetch_alerts(location_id)
        await self._fetch_shortterm_alerts(location_id)

    async def _fetch_current_weather(self, location_id: str) -> None:
        """Fetch current weather data."""
        try:
            data = await self._get_json(f"{API_WEATHER_PATH}/{location_id}")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Error fetching current weather: %s", err)
            return

        if not isinstance(data, dict):
            return

        wind_data = data.get("wind") or {}
        location_data = data.get("location") or {}

        self.current_weather_data = {
            "temperature": data.get("temperature"),
            "feels_like": data.get("feels_like"),
            "humidity": data.get("humidity"),
            "pressure": data.get("pressure"),
            "visibility": data.get("visibility"),
            "wind_speed": wind_data.get("speed"),
            "wind_deg": wind_data.get("deg"),
            "weather": data.get("weather"),
            "name": location_data.get("name"),
            "province": location_data.get("province"),
        }

    async def _fetch_forecast(self, location_id: str) -> None:
        """Fetch daily/hourly forecast data."""
        try:
            data = await self._get_json(f"{API_FORECAST_PATH}/{location_id}")
        except aiohttp.ClientError as err:
            _LOGGER.warning("Error fetching forecast: %s", err)
            return

        forecast_data = data.get("forecast", []) if isinstance(data, dict) else data
        if not isinstance(forecast_data, list):
            return

        self.daily_forecast = []
        self.hourly_forecast = []

        periods = [
            ("early_morning", "00:00"),
            ("morning", "06:00"),
            ("afternoon", "12:00"),
            ("night", "18:00"),
        ]

        for day in forecast_data:
            afternoon = day.get("afternoon") or {}
            weather_obj = afternoon.get("weather")

            self.daily_forecast.append(
                {
                    "date": day.get("date"),
                    "temp_max": day.get("temp_max"),
                    "temp_min": day.get("temp_min"),
                    "weather": weather_obj,
                }
            )

            for period_name, period_time in periods:
                period_data = day.get(period_name)
                if not isinstance(period_data, dict):
                    continue

                wind_data = period_data.get("wind") or {}
                speed_range = wind_data.get("speed_range") or []
                wind_speed = (
                    sum(speed_range) / len(speed_range)
                    if speed_range
                    else wind_data.get("speed")
                )

                self.hourly_forecast.append(
                    {
                        "date": day.get("date"),
                        "time": period_time,
                        "datetime": f"{day.get('date')}T{period_time}:00",
                        "temperature": period_data.get("temperature"),
                        "weather": period_data.get("weather"),
                        "humidity": period_data.get("humidity"),
                        "rain_prob_range": period_data.get("rain_prob_range"),
                        "wind_speed": wind_speed,
                        "wind_direction": wind_data.get("deg"),
                    }
                )

    async def _fetch_sun(self, location_id: str) -> None:
        """Fetch sunrise/sunset data."""
        try:
            data = await self._get_json(f"{API_SUN_PATH}/{location_id}")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching sun data: %s", err)
            return
        if isinstance(data, dict):
            self.sun = data.get("sun", {})

    async def _fetch_alerts(self, location_id: str) -> None:
        """Fetch weather alerts (and heat/cold warnings for the area)."""
        try:
            data = await self._get_json(f"{API_ALERT_PATH}/{location_id}")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching alerts (may be normal if none): %s", err)
            return

        if not isinstance(data, dict):
            self.alerts = {}
            return

        self.alerts = data
        area_id = data.get("area_id")
        if area_id:
            await self._fetch_heat_warnings(area_id)
            await self._fetch_cold_warnings(area_id)

    async def _fetch_shortterm_alerts(self, location_id: str) -> None:
        """Fetch avisos a muy corto plazo (short-term severe weather warnings)."""
        try:
            data = await self._get_json(f"{API_SHORTTERM_ALERT_PATH}/{location_id}")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching short-term alerts (may be normal if none): %s", err)
            self.shortterm_alerts = []
            return

        self.shortterm_alerts = data if isinstance(data, list) else []

    async def _fetch_heat_warnings(self, area_id: str) -> None:
        """Fetch heat wave warnings for the area."""
        try:
            data = await self._get_json(f"{API_HEAT_WARNING_PATH}/{area_id}")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching heat warnings (may be normal if none): %s", err)
            return
        if isinstance(data, dict):
            self.heat_warnings = data

    async def _fetch_cold_warnings(self, area_id: str) -> None:
        """Fetch cold wave warnings for the area."""
        try:
            data = await self._get_json(f"{API_COLD_WARNING_PATH}/{area_id}")
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching cold warnings (may be normal if none): %s", err)
            return
        if isinstance(data, dict):
            self.cold_warnings = data


class ArgentinaSMNDataUpdateCoordinator(DataUpdateCoordinator[ArgentinaSMNData]):
    """Class to manage fetching SMN data through the local smn-proxy add-on."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        latitude = config_entry.data[CONF_LATITUDE]
        longitude = config_entry.data[CONF_LONGITUDE]
        proxy_url = config_entry.data.get(CONF_PROXY_URL, DEFAULT_PROXY_URL)
        location_id = config_entry.data.get(CONF_LOCATION_ID)

        self._smn_data = ArgentinaSMNData(
            hass, proxy_url, latitude, longitude, location_id
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> ArgentinaSMNData:
        """Fetch data from the proxy."""
        await self._smn_data.fetch_data()
        return self._smn_data
