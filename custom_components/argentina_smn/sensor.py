"""Sensor platform: a human-readable short-term forecast/alert summary.

Synthesizes what the 11+ binary_sensors already expose (active alerts by
event, avisos a muy corto plazo, heat/cold warnings) plus today's forecast
into a single Spanish sentence, so it can be read directly in a dashboard,
a notification, or a TTS announcement without parsing individual sensors.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    PERCENTAGE,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALERT_EVENT_MAP, ALERT_LEVEL_MAP, DOMAIN, wind_cardinal
from .coordinator import ArgentinaSMNDataUpdateCoordinator
from .weather import format_condition

_LOGGER = logging.getLogger(__name__)

MAX_STATE_LENGTH = 255


def _format_time(iso_str: str | None) -> str | None:
    """Extract HH:MM from an ISO datetime string, best-effort."""
    if not iso_str or "T" not in iso_str:
        return None
    try:
        return iso_str.split("T", 1)[1][:5]
    except (IndexError, ValueError):
        return None


def _active_event_alerts(alerts: dict[str, Any]) -> list[dict[str, Any]]:
    """Return today's active alert events (max_level > 1), most severe first."""
    warnings = alerts.get("warnings") if isinstance(alerts, dict) else None
    if not warnings:
        return []

    today = warnings[0]
    events = today.get("events", [])
    active = []
    for event in events:
        max_level = event.get("max_level", 1)
        if max_level <= 1:
            continue
        event_id = event.get("id")
        event_name = ALERT_EVENT_MAP.get(event_id, f"evento_{event_id}")
        level_info = ALERT_LEVEL_MAP.get(max_level, {"name": "alerta"})
        active.append(
            {
                "event": event_name,
                "level": max_level,
                "level_name": level_info["name"],
            }
        )

    active.sort(key=lambda a: a["level"], reverse=True)
    return active


def build_summary(
    shortterm_alerts: list[dict[str, Any]],
    alerts: dict[str, Any],
    heat_warnings: dict[str, Any],
    cold_warnings: dict[str, Any],
    daily_forecast: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Build (short state text, extra attributes) summarizing the short term."""
    attrs: dict[str, Any] = {}

    # 1. Avisos a muy corto plazo (1-2h validity) take priority: most urgent.
    if shortterm_alerts:
        first = shortterm_alerts[0]
        title = (first.get("title") or "").strip().rstrip(".").capitalize()
        end_time = _format_time(first.get("end_date"))
        zones = first.get("zones") or []
        zone_text = zones[0] if zones else ""

        state = f"⚠️ {title}"
        if end_time:
            state += f" (vigente hasta las {end_time})"
        if zone_text:
            state = f"{state}. {zone_text}"

        attrs["avisos_corto_plazo"] = [
            {
                "titulo": a.get("title"),
                "emitido": a.get("date"),
                "vigente_hasta": a.get("end_date"),
                "zonas": a.get("zones"),
                "region": a.get("region"),
                "instrucciones": a.get("instructions"),
            }
            for a in shortterm_alerts
        ]
        # Full protective-measures text for the most urgent aviso, straight
        # from the SMN API (warning/shortterm's "instructions" field) — no
        # need to scrape the website's HTML for this, it's already in the
        # JSON we fetch.
        if first.get("instructions"):
            attrs["instrucciones"] = first["instructions"]
        return state[:MAX_STATE_LENGTH], attrs

    # 2. Alertas por evento (tormenta, lluvia, viento, etc.) para hoy.
    active_events = _active_event_alerts(alerts)
    if active_events:
        parts = [f"{a['event'].replace('_', ' ')} ({a['level_name']})" for a in active_events]
        state = "Alerta de " + ", ".join(parts) + " para hoy"
        attrs["alertas_activas"] = active_events
        return state[:MAX_STATE_LENGTH], attrs

    # 3. Olas de calor/frío.
    heat_level = heat_warnings.get("level", 1) if heat_warnings else 1
    cold_level = cold_warnings.get("level", 1) if cold_warnings else 1
    if heat_level > 1:
        return f"Alerta por ola de calor (nivel {heat_level})"[:MAX_STATE_LENGTH], attrs
    if cold_level > 1:
        return f"Alerta por ola de frío (nivel {cold_level})"[:MAX_STATE_LENGTH], attrs

    # 4. Sin alertas: resumen del pronóstico de hoy.
    if daily_forecast:
        today = daily_forecast[0]
        condition = format_condition(today.get("weather"))
        temp_max = today.get("temp_max")
        temp_min = today.get("temp_min")
        state = "Sin alertas vigentes."
        if temp_max is not None and temp_min is not None:
            state += f" Hoy: {condition}, máxima {temp_max:.0f}°C / mínima {temp_min:.0f}°C"
        return state[:MAX_STATE_LENGTH], attrs

    return "Sin alertas vigentes", attrs


def build_nationwide_summary(
    nationwide_alerts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Build (short state text, extra attributes) for the nationwide avisos.

    Groups the current avisos a muy corto plazo (no location filter) by
    province — the same view as smn.gob.ar's "Resumen por provincia".
    """
    if not nationwide_alerts:
        return "Sin avisos vigentes en el país", {"por_provincia": {}}

    by_province: dict[str, list[dict[str, Any]]] = {}
    for alert in nationwide_alerts:
        provinces = alert.get("provinces") or []
        province_names = [p.get("name") for p in provinces if p.get("name")]
        if not province_names:
            # Fall back to parsing "PROVINCIA: departamentos..." from zones.
            province_names = [
                z.split(":", 1)[0].strip().title() for z in (alert.get("zones") or [])
            ]

        entry = {
            "titulo": alert.get("title"),
            "vigente_hasta": alert.get("end_date"),
            "zonas": alert.get("zones"),
        }
        for name in province_names or ["(sin provincia)"]:
            by_province.setdefault(name, []).append(entry)

    province_list = sorted(by_province.keys())
    state = f"{len(nationwide_alerts)} avisos vigentes en {', '.join(province_list)}"

    return state[:MAX_STATE_LENGTH], {
        "total_avisos": len(nationwide_alerts),
        "por_provincia": by_province,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN summary sensors."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    async_add_entities(
        [
            SMNShortTermSummarySensor(coordinator, config_entry),
            SMNNationwideAvisosSensor(coordinator, config_entry),
            SMNTemperatureSensor(coordinator, config_entry),
            SMNFeelsLikeSensor(coordinator, config_entry),
            SMNHumiditySensor(coordinator, config_entry),
            SMNWindSpeedSensor(coordinator, config_entry),
            SMNWindBearingSensor(coordinator, config_entry),
            SMNTodayForecastSensor(coordinator, config_entry),
            SMNTomorrowForecastSensor(coordinator, config_entry),
        ]
    )


class _SMNSensorBase(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity):
    """Shared device_info boilerplate for the standalone current/forecast sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        unique_id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}{unique_id_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )


class SMNTemperatureSensor(_SMNSensorBase):
    """Current temperature, as its own entity (separate from the weather entity)."""

    _attr_translation_key = "current_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("temperature")


class SMNFeelsLikeSensor(_SMNSensorBase):
    """Current apparent ("feels like") temperature."""

    _attr_translation_key = "feels_like_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_feels_like_temperature")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("feels_like")


class SMNHumiditySensor(_SMNSensorBase):
    """Current relative humidity."""

    _attr_translation_key = "current_humidity"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_humidity")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("humidity")


class SMNWindSpeedSensor(_SMNSensorBase):
    """Current wind speed."""

    _attr_translation_key = "current_wind_speed"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfSpeed.KILOMETERS_PER_HOUR

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_wind_speed")

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.current_weather_data.get("wind_speed")


class SMNWindBearingSensor(_SMNSensorBase):
    """Current wind direction, as a 16-point compass label (e.g. "NE").

    The degree value is kept as an attribute rather than the state, since a
    compass label reads better on a dashboard/automation than a raw number.
    """

    _attr_translation_key = "current_wind_bearing"
    _attr_icon = "mdi:compass-outline"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_current_wind_bearing")

    @property
    def native_value(self) -> str | None:
        return wind_cardinal(self.coordinator.data.current_weather_data.get("wind_deg"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        degrees = self.coordinator.data.current_weather_data.get("wind_deg")
        return {"degrees": degrees} if degrees is not None else {}


class _SMNDailyForecastSensor(_SMNSensorBase):
    """Shared logic for a single day's forecast (today or tomorrow) as a sensor.

    State is the day's condition (e.g. "sunny"); max/min temperature and the
    rest of the day's data are exposed as attributes, since a sensor only
    has one state value but dashboards/automations may want the numbers too.
    """

    _day_index: int

    @property
    def _day(self) -> dict[str, Any] | None:
        forecast = self.coordinator.data.daily_forecast
        if not forecast or len(forecast) <= self._day_index:
            return None
        return forecast[self._day_index]

    @property
    def native_value(self) -> str | None:
        day = self._day
        if day is None:
            return None
        return format_condition(day.get("weather"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        day = self._day
        if day is None:
            return {}
        return {
            "temp_max": day.get("temp_max"),
            "temp_min": day.get("temp_min"),
            "date": day.get("date"),
        }


class SMNTodayForecastSensor(_SMNDailyForecastSensor):
    """Today's forecast condition, with max/min temperature as attributes."""

    _attr_translation_key = "today_forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _day_index = 0

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_today_forecast")


class SMNTomorrowForecastSensor(_SMNDailyForecastSensor):
    """Tomorrow's forecast condition, with max/min temperature as attributes."""

    _attr_translation_key = "tomorrow_forecast"
    _attr_icon = "mdi:weather-partly-cloudy"
    _day_index = 1

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_tomorrow_forecast")


class SMNShortTermSummarySensor(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity
):
    """Human-readable short-term forecast/alert summary, in Spanish."""

    _attr_has_entity_name = True
    _attr_translation_key = "short_term_summary"
    _attr_icon = "mdi:text-box-outline"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_short_term_summary"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        data = self.coordinator.data
        state, _ = build_summary(
            data.shortterm_alerts,
            data.alerts,
            data.heat_warnings,
            data.cold_warnings,
            data.daily_forecast,
        )
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        _, attrs = build_summary(
            data.shortterm_alerts,
            data.alerts,
            data.heat_warnings,
            data.cold_warnings,
            data.daily_forecast,
        )
        return attrs


class SMNNationwideAvisosSensor(
    CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], SensorEntity
):
    """Avisos a muy corto plazo for the whole country, grouped by province.

    Same view as smn.gob.ar's "Resumen por provincia" — not filtered to
    the configured location, so it's shared/identical across every SMN
    config entry in this Home Assistant instance.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "nationwide_avisos"
    _attr_icon = "mdi:map-marker-alert-outline"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_nationwide_avisos"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        state, _ = build_nationwide_summary(self.coordinator.data.nationwide_shortterm_alerts)
        return state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _, attrs = build_nationwide_summary(self.coordinator.data.nationwide_shortterm_alerts)
        return attrs
