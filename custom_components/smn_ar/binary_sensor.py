"""Binary sensor platform for SMN weather alerts.

Alerts come in two flavors, depending on what SMN publishes for each type:

- By proximity (storm, rain, wind, snow, zonda wind, hail, and the generic
  "Alerta a corto plazo"): SMN issues these as avisos a muy corto plazo
  with a polygon, so they turn on when a matching aviso is within the
  radius configured in the integration's options.
- By zone (temperatures, fog, dust, smoke, volcanic ash, and the summary
  "Alerta meteorológica"): SMN only forecasts these for the whole alert
  zone the location belongs to, with a level per period of the day — they
  turn on during the periods SMN forecasts an alert for.

Every alert exposes a "criterio" attribute explaining, in Spanish, what
turns it on.
"""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .alert_levels import (
    PERIOD_START_UTC_HOURS,
    active_events_now,
    current_period,
    event_levels,
    get_alert_radius_km,
    level_report,
    ranked_avisos,
    todays_warning,
)
from .const import (
    ALERT_EVENT_ICONS,
    ALERT_EVENT_LABELS_ES,
    ALERT_EVENT_MAP,
    ALERT_EVENT_NEARBY_KEYWORDS,
    ALERT_LEVEL_MAP,
    DOMAIN,
    HAIL_NEARBY_KEYWORDS,
    wind_cardinal,
)
from .coordinator import ArgentinaSMNDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Event types for Home Assistant events
EVENT_ALERT_CREATED = f"{DOMAIN}_alert_created"
EVENT_ALERT_UPDATED = f"{DOMAIN}_alert_updated"
EVENT_ALERT_CLEARED = f"{DOMAIN}_alert_cleared"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SMN binary sensors."""
    coordinator: ArgentinaSMNDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]

    # "Granizo cercano" was merged into "Alerta por granizo" (which now
    # works by proximity itself): drop its leftover registry entry.
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{config_entry.entry_id}_nearby_hail"
    ):
        registry.async_remove(entity_id)

    entities: list[BinarySensorEntity] = [SMNAlertSensor(coordinator, config_entry)]
    for event_id, event_name in ALERT_EVENT_MAP.items():
        if event_name in ALERT_EVENT_NEARBY_KEYWORDS:
            entities.append(SMNNearbyEventAlertSensor(coordinator, config_entry, event_id, event_name))
        else:
            entities.append(SMNZoneEventAlertSensor(coordinator, config_entry, event_id, event_name))
    entities.append(SMNShortTermAlertSensor(coordinator, config_entry))
    entities.append(SMNHailAlertSensor(coordinator, config_entry))

    async_add_entities(entities)


def _format_km(value: float) -> str:
    return f"{value:g}"


def _aviso_summary(distance: float, bearing: float, alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "distance_km": round(distance, 1),
        "direction": None if distance == 0 else wind_cardinal(bearing),
        "title": (alert.get("title") or "").strip(),
        "end_date": alert.get("end_date"),
        "zones": alert.get("zones"),
    }


class _SMNAlertBase(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], BinarySensorEntity):
    """Shared device info, plus an optional state refresh when the period of the day changes."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True
    # Zone alerts follow SMN's per-period levels, which change at fixed
    # times rather than on a coordinator refresh.
    _refresh_on_period_change = False

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._refresh_on_period_change:
            self.async_on_remove(
                async_track_utc_time_change(
                    self.hass,
                    self._handle_period_change,
                    hour=PERIOD_START_UTC_HOURS,
                    minute=0,
                    second=5,
                )
            )

    @callback
    def _handle_period_change(self, _now: datetime) -> None:
        self.async_write_ha_state()


class _SMNNearbyAlertBase(_SMNAlertBase):
    """On when a short-term warning (aviso) matching some keywords is within the configured radius.

    Uses the nationwide warning/shortterm list, whose avisos come with SMN's
    own polygon: distance 0 means the location is inside it.
    """

    _keywords: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    @property
    def _radius_km(self) -> float:
        return get_alert_radius_km(self._config_entry)

    def _ranked(self) -> list[tuple[float, float, dict[str, Any]]]:
        """Matching avisos as (distance, bearing, aviso), nearest first."""
        return ranked_avisos(
            self._config_entry.data[CONF_LATITUDE],
            self._config_entry.data[CONF_LONGITUDE],
            self.coordinator.data.nationwide_shortterm_alerts,
            self._keywords,
        )

    @property
    def is_on(self) -> bool:
        ranked = self._ranked()
        return bool(ranked) and ranked[0][0] <= self._radius_km

    def _nearby_attributes(self) -> dict[str, Any]:
        """Criterio, radius and the nearest matching aviso (even when outside the radius)."""
        attrs: dict[str, Any] = {"criterio": self._criterio(), "radius_km": self._radius_km}
        ranked = self._ranked()
        if ranked:
            attrs.update(_aviso_summary(*ranked[0]))
        return attrs

    def _criterio(self) -> str:
        raise NotImplementedError


class SMNNearbyEventAlertSensor(_SMNNearbyAlertBase):
    """Alerta por tormenta / lluvia / viento / nevada / viento zonda: by proximity.

    SMN's zone forecast for the same event is kept as context in the
    zone_* attributes — it doesn't turn the sensor on, since the zone can be
    far larger than the radius.
    """

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        event_id: int,
        event_name: str,
    ) -> None:
        super().__init__(coordinator, config_entry, f"_alert_{event_name}")
        self._event_id = event_id
        self._event_name = event_name
        self._keywords = ALERT_EVENT_NEARBY_KEYWORDS[event_name]
        self._attr_translation_key = f"alert_{event_name}"
        self._attr_icon = ALERT_EVENT_ICONS.get(event_name, "mdi:alert")

    def _criterio(self) -> str:
        label = ALERT_EVENT_LABELS_ES.get(self._event_name, self._event_name)
        return (
            f"Se enciende si hay un aviso a corto plazo del SMN por {label} "
            f"a menos de {_format_km(self._radius_km)} km de tu ubicación"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        level_now, max_today, levels = event_levels(self.coordinator.data.alerts, self._event_id)
        return {
            "event_id": self._event_id,
            "event_name": self._event_name,
            **self._nearby_attributes(),
            "zone_level_now": level_now,
            "zone_level_name_now": ALERT_LEVEL_MAP.get(level_now, ALERT_LEVEL_MAP[1])["name"],
            "zone_max_level_today": max_today,
            "zone_levels": levels,
        }


class SMNZoneEventAlertSensor(_SMNAlertBase):
    """Alerta por temperaturas / niebla / polvo / humo / ceniza: SMN's zone forecast, current period."""

    _refresh_on_period_change = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        event_id: int,
        event_name: str,
    ) -> None:
        super().__init__(coordinator, config_entry, f"_alert_{event_name}")
        self._event_id = event_id
        self._event_name = event_name
        self._attr_translation_key = f"alert_{event_name}"
        self._attr_icon = ALERT_EVENT_ICONS.get(event_name, "mdi:alert")

    @property
    def is_on(self) -> bool:
        return event_levels(self.coordinator.data.alerts, self._event_id)[0] > 1

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        alerts = self.coordinator.data.alerts
        _, _, period_label = current_period()
        label = ALERT_EVENT_LABELS_ES.get(self._event_name, self._event_name)
        level, max_today, levels = event_levels(alerts, self._event_id)
        level_info = ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])
        # Off now but forecast later today: describe the day's highest level.
        description, instruction = level_report(alerts, self._event_id, level if level > 1 else max_today)
        warning = todays_warning(alerts)
        return {
            "event_id": self._event_id,
            "event_name": self._event_name,
            "criterio": (
                f"Se enciende si el SMN tiene alerta por {label} para tu zona "
                f"en la franja actual ({period_label})"
            ),
            "level": level,
            "level_name": level_info["name"],
            "color": level_info["color"],
            "severity": level_info["severity"],
            "period": period_label,
            "max_level_today": max_today,
            "levels": levels,
            "date": warning.get("date") if warning else None,
            "description": description,
            "instruction": instruction,
        }


class SMNAlertSensor(_SMNAlertBase):
    """Alerta meteorológica: any SMN zone alert in the current period.

    This is the anticipation sensor — SMN's zone forecast, issued hours
    ahead, for every event type. It doesn't measure distance (the zone is
    the finest unit SMN forecasts for); the proximity alerts do.
    """

    _attr_translation_key = "weather_alert"
    _attr_icon = "mdi:alert"
    _refresh_on_period_change = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, "_alert")
        self._previous_alerts: set[tuple[int, int]] = set()  # (event_id, level)

    @property
    def is_on(self) -> bool:
        """Return True if the zone has an alert in the current period."""
        return bool(active_events_now(self.coordinator.data.alerts))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        alerts = self.coordinator.data.alerts or {}
        _, _, period_label = current_period()
        attrs: dict[str, Any] = {
            "criterio": (
                "Se enciende si el SMN tiene alguna alerta para tu zona en la franja "
                f"actual ({period_label}). Es el pronóstico por zona: anticipa horas "
                "antes, pero no mide distancia"
            ),
            "period": period_label,
        }

        active_alerts = []
        max_level = 1
        for event in active_events_now(alerts):
            event_id = event["event_id"]
            level_info = ALERT_LEVEL_MAP.get(event["level"], ALERT_LEVEL_MAP[1])
            description, _ = level_report(alerts, event_id, event["level"])
            active_alerts.append({
                "event_name": ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}"),
                "level": event["level"],
                "level_name": level_info["name"],
                "severity": level_info["severity"],
                "max_level_today": event["max_level"],
                "description": description,
            })
            max_level = max(max_level, event["level"])

        # Everything forecast for today, including periods other than the
        # current one — to see what's coming later.
        warning = todays_warning(alerts)
        today_alerts = [
            {
                "event_name": ALERT_EVENT_MAP.get(event.get("id"), f"unknown_{event.get('id')}"),
                "max_level": event.get("max_level", 1),
                "levels": event.get("levels"),
            }
            for event in (warning or {}).get("events", [])
            if event.get("max_level", 1) > 1
        ]

        max_severity_info = ALERT_LEVEL_MAP.get(max_level, ALERT_LEVEL_MAP[1])
        attrs.update(
            {
                "active_alert_count": len(active_alerts),
                "max_severity": max_severity_info["severity"],
                "max_level": max_level,
                "alert_summary": ", ".join(f"{a['event_name']} ({a['level_name']})" for a in active_alerts),
                "active_alerts": active_alerts,
                "today_alerts": today_alerts,
                "area_id": alerts.get("area_id"),
                "updated": alerts.get("updated"),
            }
        )
        return attrs

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Fire events for alert changes
        self._fire_alert_events()
        super()._handle_coordinator_update()

    @callback
    def _handle_period_change(self, _now: datetime) -> None:
        self._fire_alert_events()
        super()._handle_period_change(_now)

    def _fire_alert_events(self) -> None:
        """Fire Home Assistant events for alert changes (current period)."""
        alerts = self.coordinator.data.alerts
        current_alerts: set[tuple[int, int]] = {
            (event["event_id"], event["level"]) for event in active_events_now(alerts)
        }

        # Find new alerts
        previous_ids = {eid for eid, _ in self._previous_alerts}
        for event_id, level in current_alerts:
            if event_id in previous_ids:
                continue
            event_name = ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}")
            level_info = ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])
            description, _ = level_report(alerts, event_id, level)

            self.hass.bus.fire(
                EVENT_ALERT_CREATED,
                {
                    "event_id": event_id,
                    "event_name": event_name,
                    "level": level,
                    "level_name": level_info["name"],
                    "severity": level_info["severity"],
                    "description": description,
                },
            )
            _LOGGER.info(
                "Alert created: %s (level %d - %s)",
                event_name,
                level,
                level_info["name"],
            )

        # Find updated alerts (level changed)
        for event_id, level in current_alerts:
            old_level = next((l for eid, l in self._previous_alerts if eid == event_id), None)
            if old_level and old_level != level:
                event_name = ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}")
                level_info = ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])

                self.hass.bus.fire(
                    EVENT_ALERT_UPDATED,
                    {
                        "event_id": event_id,
                        "event_name": event_name,
                        "old_level": old_level,
                        "new_level": level,
                        "level_name": level_info["name"],
                        "severity": level_info["severity"],
                    },
                )
                _LOGGER.info(
                    "Alert updated: %s (level %d → %d)",
                    event_name,
                    old_level,
                    level,
                )

        # Find cleared alerts
        current_ids = {eid for eid, _ in current_alerts}
        for event_id, level in self._previous_alerts:
            if event_id in current_ids:
                continue
            event_name = ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}")

            self.hass.bus.fire(
                EVENT_ALERT_CLEARED,
                {
                    "event_id": event_id,
                    "event_name": event_name,
                    "level": level,
                },
            )
            _LOGGER.info("Alert cleared: %s (level %d)", event_name, level)

        # Update previous alerts
        self._previous_alerts = current_alerts


class SMNShortTermAlertSensor(_SMNNearbyAlertBase):
    """Alerta a corto plazo: any short-term warning (aviso) within the configured radius."""

    _attr_translation_key = "short_term_alert"
    _attr_icon = "mdi:alert-circle"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, config_entry, "_shortterm_alert")

    def _criterio(self) -> str:
        return (
            "Se enciende si hay cualquier aviso a corto plazo del SMN a menos de "
            f"{_format_km(self._radius_km)} km de tu ubicación"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        radius = self._radius_km
        within = [item for item in self._ranked() if item[0] <= radius]
        return {
            **self._nearby_attributes(),
            "alert_count": len(within),
            "alerts": [
                {
                    **_aviso_summary(distance, bearing, alert),
                    "date": alert.get("date"),
                    "severity": alert.get("severity"),
                    "instructions": alert.get("instructions"),
                    "region": alert.get("region"),
                }
                for distance, bearing, alert in within
            ],
        }


_HAIL_KEYWORD = "granizo"
_HAIL_TORMENTA_EVENT_ID = 41  # ALERT_EVENT_MAP[41] == "tormenta"


def _zone_hail_mentions(alerts_data: dict[str, Any]) -> list[str]:
    """Hail mentions in today's "tormenta" zone alert level description.

    That text is SMN's generic description for the alert *level* (e.g.
    "...ocasional granizo..." appears in many yellow storm alerts), not a
    forecast of hail at a specific point — so it's reported as context, not
    used to turn the sensor on.
    """
    warning = todays_warning(alerts_data)
    if warning is None:
        return []
    active_level = next(
        (
            event.get("max_level", 1)
            for event in warning.get("events", [])
            if event.get("id") == _HAIL_TORMENTA_EVENT_ID and event.get("max_level", 1) > 1
        ),
        None,
    )
    if not active_level:
        return []
    mentions = []
    for report in (alerts_data or {}).get("reports") or []:
        if report.get("event_id") != _HAIL_TORMENTA_EVENT_ID:
            continue
        for level_data in report.get("levels", []):
            description = level_data.get("description") or ""
            if level_data.get("level") == active_level and _HAIL_KEYWORD in description.lower():
                mentions.append(description.strip())
    return mentions


class SMNHailAlertSensor(_SMNNearbyAlertBase):
    """Alerta por granizo: a short-term warning that mentions hail within the configured radius.

    SMN's warning API has no dedicated hail event, so this matches "granizo"
    in the avisos' titles. Replaces the former separate "Granizo cercano".
    """

    _attr_translation_key = "hail_alert"
    _attr_icon = "mdi:weather-hail"
    _keywords = HAIL_NEARBY_KEYWORDS

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_hail_alert")

    def _criterio(self) -> str:
        return (
            "Se enciende si hay un aviso a corto plazo del SMN que menciona granizo "
            f"a menos de {_format_km(self._radius_km)} km de tu ubicación"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            **self._nearby_attributes(),
            "zone_alert_mentions": _zone_hail_mentions(self.coordinator.data.alerts),
        }
