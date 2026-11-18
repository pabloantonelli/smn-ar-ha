"""The devices each location's entities are grouped in.

Five devices per location instead of one, so each entity's name can be short
and say what it is ("Tormenta" under "Avisos cercanos"). The current weather
keeps the original device (identifier: the entry id), so upgrading moves the
other entities out of it without leaving an empty one behind.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN

CURRENT = "current"
FORECAST = "forecast"
NEARBY = "nearby"
ZONE = "zone"
MAPS = "maps"

# Built here rather than with a device translation_key: translated device
# names with placeholders need a newer Home Assistant than hacs.json allows.
_LABELS = {
    "es": {
        CURRENT: "Clima actual",
        FORECAST: "Pronóstico",
        NEARBY: "Avisos cercanos",
        ZONE: "Alertas de la zona",
        MAPS: "Mapas",
    },
    "en": {
        CURRENT: "Current weather",
        FORECAST: "Forecast",
        NEARBY: "Nearby warnings",
        ZONE: "Zone alerts",
        MAPS: "Maps",
    },
}


def device_info(hass: HomeAssistant | None, config_entry: ConfigEntry, group: str) -> DeviceInfo:
    """The device for one group of a location's entities."""
    language = "es" if hass is None or (hass.config.language or "es").startswith("es") else "en"
    location = config_entry.data.get(CONF_NAME, "SMN")
    info = DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id if group == CURRENT else f"{config_entry.entry_id}_{group}")},
        name=f"{location} · {_LABELS[language][group]}",
        manufacturer="Servicio Meteorológico Nacional",
        entry_type=DeviceEntryType.SERVICE,
    )
    if group != CURRENT:
        info["via_device"] = (DOMAIN, config_entry.entry_id)
    return info
