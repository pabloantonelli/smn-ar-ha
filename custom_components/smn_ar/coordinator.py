"""DataUpdateCoordinator for the SMN integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
import time
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
    API_SHORTTERM_NATIONWIDE_PATH,
    API_SUN_PATH,
    API_WEATHER_PATH,
    CONF_LOCATION_ID,
    CONF_PROXY_URL,
    DEFAULT_PROXY_URL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_SHORTTERM_ALERT_CHANGED,
    NOWCAST_UPDATE_INTERVAL,
    OPEN_METEO_FORECAST_DAYS,
    OPEN_METEO_HOURLY_FIELDS,
    OPEN_METEO_URL,
    SHORTTERM_SCAN_INTERVAL,
)
from .nowcast import (
    Nowcast,
    fetch_infrared_field,
    fetch_radar_field,
    fetch_sinarame_field,
    radar_covers,
    sinarame_radars_for,
    run_nowcast,
    steering_motion,
)

_LOGGER = logging.getLogger(__name__)


def _estimate_feels_like(
    temperature: float | None, humidity: float | None, wind_speed_kmh: float | None
) -> float | None:
    """Estimate apparent temperature when SMN doesn't send `feels_like` itself.

    SMN's own API returns `null` for `feels_like` under many conditions
    (not just extreme heat/cold — mild days get it too), so relying only
    on their value leaves the sensor "Unknown" most of the time. This is
    the Australian Bureau of Meteorology's general Apparent Temperature
    formula (AT = T + 0.33*e - 0.70*ws - 4.00, e = vapor pressure from
    humidity) — a standard approximation that accounts for both heat
    (humidity) and wind-chill effects in one continuous formula, using
    only the fields SMN always provides. It's an estimate, not SMN's own
    calculation — callers should treat it as such (see `feels_like_is_estimate`
    in current_weather_data).
    """
    if temperature is None or humidity is None or wind_speed_kmh is None:
        return None
    wind_ms = wind_speed_kmh / 3.6
    vapor_pressure = (humidity / 100) * 6.105 * math.exp(17.27 * temperature / (237.7 + temperature))
    apparent = temperature + 0.33 * vapor_pressure - 0.70 * wind_ms - 4.00
    return round(apparent, 1)


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
        self.nationwide_shortterm_alerts: list[dict[str, Any]] = []
        self.heat_warnings: dict[str, Any] = {}
        self.cold_warnings: dict[str, Any] = {}
        self.sun: dict[str, Any] = {}
        # Open-Meteo's hourly forecast (see _fetch_open_meteo_hourly).
        self.open_meteo_hourly: list[dict[str, Any]] = []
        self._last_full_fetch: float | None = None

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
        """Fetch SMN data through the proxy.

        Called every SHORTTERM_SCAN_INTERVAL: the avisos a muy corto plazo
        (valid 1-2h) are fetched every time, everything else only once
        DEFAULT_SCAN_INTERVAL has passed, matching SMN's own cadence for it.
        """
        try:
            location_id = await self._get_location_id()
        except UpdateFailed:
            raise
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Error resolving location: {err}") from err

        now = time.monotonic()
        # A minute of slack: polls land at ~10-min steps, and the third one
        # must count as "30 min elapsed" even if it fires slightly early.
        if self._last_full_fetch is None or now - self._last_full_fetch >= DEFAULT_SCAN_INTERVAL - 60:
            await self._fetch_current_weather(location_id)
            await self._fetch_forecast(location_id)
            await self._fetch_sun(location_id)
            await self._fetch_alerts(location_id)
            await self._fetch_open_meteo_hourly()
            self._last_full_fetch = now
        await self._fetch_shortterm_alerts(location_id)
        await self._fetch_nationwide_shortterm_alerts()

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

        temperature = data.get("temperature")
        humidity = data.get("humidity")
        wind_speed = wind_data.get("speed")
        feels_like = data.get("feels_like")
        feels_like_is_estimate = feels_like is None
        if feels_like_is_estimate:
            feels_like = _estimate_feels_like(temperature, humidity, wind_speed)

        self.current_weather_data = {
            "temperature": temperature,
            "feels_like": feels_like,
            "feels_like_is_estimate": feels_like_is_estimate,
            "humidity": humidity,
            "pressure": data.get("pressure"),
            "visibility": data.get("visibility"),
            "wind_speed": wind_speed,
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

    async def _fetch_open_meteo_hourly(self) -> None:
        """Fetch Open-Meteo's hourly forecast for the location (OPEN_METEO_HOURLY_FIELDS).

        Not SMN and not through the proxy (see OPEN_METEO_URL). Keeps the
        previous data on failure: everything using it falls back to SMN's.
        """
        params = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "hourly": ",".join(OPEN_METEO_HOURLY_FIELDS.values()),
            "timeformat": "unixtime",
            "forecast_days": OPEN_METEO_FORECAST_DAYS,
        }
        try:
            async with async_timeout.timeout(10):
                response = await self._session.get(OPEN_METEO_URL, params=params)
                response.raise_for_status()
                data = await response.json()
            hourly = data["hourly"]
            columns = [hourly[name] for name in OPEN_METEO_HOURLY_FIELDS.values()]
            self.open_meteo_hourly = [
                {
                    "when": datetime.fromtimestamp(timestamp, timezone.utc),
                    **dict(zip(OPEN_METEO_HOURLY_FIELDS, values)),
                }
                for timestamp, *values in zip(hourly["time"], *columns)
            ]
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as err:
            _LOGGER.debug("Error fetching Open-Meteo hourly forecast: %s", err)

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

    @staticmethod
    def _shortterm_alert_signature(alert: dict[str, Any]) -> tuple[Any, ...]:
        """Identity of a single aviso, used to detect additions/removals.

        There's no stable numeric id in the API response, so (title, date,
        end_date) stands in for one — two avisos with the same title issued
        at the same time are the same aviso for this purpose.
        """
        return (alert.get("title"), alert.get("date"), alert.get("end_date"))

    async def _fetch_nationwide_shortterm_alerts(self) -> None:
        """Fetch avisos a muy corto plazo for the whole country (no location filter)."""
        try:
            data = await self._get_json(API_SHORTTERM_NATIONWIDE_PATH)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Error fetching nationwide short-term alerts: %s", err)
            self.nationwide_shortterm_alerts = []
            return

        self.nationwide_shortterm_alerts = data if isinstance(data, list) else []

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
        self._config_entry = config_entry
        self._previous_shortterm_signatures: set[tuple[Any, ...]] | None = None
        # Set up in async_setup_entry, once this one has its first data.
        self.nowcast: SMNNowcastCoordinator | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SHORTTERM_SCAN_INTERVAL),
        )

    async def _async_update_data(self) -> ArgentinaSMNData:
        """Fetch data from the proxy."""
        await self._smn_data.fetch_data()
        self._fire_shortterm_alert_event_if_changed()
        return self._smn_data

    def _fire_shortterm_alert_event_if_changed(self) -> None:
        """Fire EVENT_SHORTTERM_ALERT_CHANGED if the avisos vigentes changed.

        Skips firing on the very first refresh (no previous state to diff
        against) so startup doesn't fire a burst of "new" events for avisos
        that were already active before Home Assistant started.
        """
        current = self._smn_data.shortterm_alerts
        current_signatures = {
            ArgentinaSMNData._shortterm_alert_signature(alert) for alert in current
        }

        if self._previous_shortterm_signatures is None:
            self._previous_shortterm_signatures = current_signatures
            return

        if current_signatures == self._previous_shortterm_signatures:
            return

        previous_signatures = self._previous_shortterm_signatures
        self._previous_shortterm_signatures = current_signatures

        added = [
            alert
            for alert in current
            if ArgentinaSMNData._shortterm_alert_signature(alert) not in previous_signatures
        ]
        removed_signatures = previous_signatures - current_signatures

        _LOGGER.info(
            "Avisos a muy corto plazo changed: %d added, %d removed",
            len(added),
            len(removed_signatures),
        )
        self.hass.bus.async_fire(
            EVENT_SHORTTERM_ALERT_CHANGED,
            {
                "entry_id": self._config_entry.entry_id,
                "added": added,
                "removed_count": len(removed_signatures),
                "current": current,
            },
        )


class SMNNowcastCoordinator(DataUpdateCoordinator[Nowcast | None]):
    """Nowcast for the location (see nowcast.py), every NOWCAST_UPDATE_INTERVAL.

    The nearest SINARAME radar; else RainViewer's radar where it has
    coverage; else the infrared. None when none could be fetched. Separate
    from the SMN coordinator: different sources and cadence, and a failure
    here must never take the SMN data down.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        smn_coordinator: ArgentinaSMNDataUpdateCoordinator,
    ) -> None:
        self._latitude = config_entry.data[CONF_LATITUDE]
        self._longitude = config_entry.data[CONF_LONGITUDE]
        self._smn_coordinator = smn_coordinator
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_nowcast",
            update_interval=timedelta(seconds=NOWCAST_UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> Nowcast | None:
        try:
            import numpy  # noqa: F401
        except ImportError:
            _LOGGER.warning("numpy not available, no nowcast for the next rain")
            return None
        session = async_get_clientsession(self.hass)
        smn_data = self._smn_coordinator.data
        steering = steering_motion(smn_data.open_meteo_hourly if smn_data else None)
        lat, lon = self._latitude, self._longitude

        async def rainviewer():
            if await radar_covers(session, lat, lon):
                return await fetch_radar_field(session, lat, lon)
            return None

        # Most precise first; the next one when it has nothing to say (a
        # SINARAME radar whose frames are all artifacts, say).
        for fetch in (
            *(
                lambda radar_id=radar_id: fetch_sinarame_field(session, radar_id, lat, lon)
                for radar_id in sinarame_radars_for(lat, lon)
            ),
            rainviewer,
            lambda: fetch_infrared_field(session, lat, lon),
        ):
            try:
                field = await fetch()
            except (aiohttp.ClientError, asyncio.TimeoutError, KeyError) as err:
                _LOGGER.debug("Error fetching nowcast frames: %s", err)
                continue
            if field is None:
                continue
            try:
                nowcast = await self.hass.async_add_executor_job(run_nowcast, field, steering)
            except Exception:  # noqa: BLE001 - e.g. an image format that changed: try the next source
                _LOGGER.warning("Error computing the nowcast from %s", field.source, exc_info=True)
                continue
            if nowcast is not None:
                return nowcast
        return None
