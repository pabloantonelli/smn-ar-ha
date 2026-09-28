"""Sensor platform: a human-readable short-term forecast/alert summary.

Synthesizes what the 11+ binary_sensors already expose (active alerts by
event, avisos a muy corto plazo, heat/cold warnings) plus today's forecast
into a single Spanish sentence, so it can be read directly in a dashboard,
a notification, or a TTS announcement without parsing individual sensors.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALERT_EVENT_MAP, ALERT_LEVEL_MAP, DOMAIN
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


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the SMN short-term summary sensor."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    async_add_entities([SMNShortTermSummarySensor(coordinator, config_entry)])


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
