"""Binary sensor platform for SMN weather alerts."""
from __future__ import annotations

import logging
import math
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ALERT_EVENT_ICONS,
    ALERT_EVENT_MAP,
    ALERT_LEVEL_MAP,
    CONF_HAIL_RADIUS_KM,
    DEFAULT_HAIL_RADIUS_KM,
    DOMAIN,
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

    entities: list[BinarySensorEntity] = []

    # Main alert sensor
    entities.append(SMNAlertSensor(coordinator, config_entry))

    # Individual event type sensors
    for event_id, event_name in ALERT_EVENT_MAP.items():
        entities.append(SMNEventAlertSensor(coordinator, config_entry, event_id, event_name))

    # Short-term alert sensor
    entities.append(SMNShortTermAlertSensor(coordinator, config_entry))

    # Hail: SMN has no dedicated event id for it (unlike rain/dust/ash/etc.),
    # so this is derived by text-matching "granizo" instead — see
    # SMNHailAlertSensor's docstring.
    entities.append(SMNHailAlertSensor(coordinator, config_entry))
    entities.append(SMNNearbyHailSensor(coordinator, config_entry))

    async_add_entities(entities)


class SMNAlertSensor(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], BinarySensorEntity):
    """Binary sensor for SMN weather alerts (all types)."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True
    _attr_translation_key = "weather_alert"
    _attr_icon = "mdi:alert"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_alert"
        self._previous_alerts: set[tuple[int, int]] = set()  # (event_id, level)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Return True if there are active alerts."""
        if not self.coordinator.data.alerts:
            return False

        warnings = self.coordinator.data.alerts.get("warnings", [])
        if not warnings or len(warnings) == 0:
            return False

        # Check if any event has level > 1
        current_warning = warnings[0]
        events = current_warning.get("events", [])
        return any(event.get("max_level", 1) > 1 for event in events)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data.alerts:
            return {}

        warnings = self.coordinator.data.alerts.get("warnings", [])
        reports = self.coordinator.data.alerts.get("reports", [])

        if not warnings or len(warnings) == 0:
            return {
                "active_alert_count": 0,
                "max_severity": "info",
                "max_level": 1,
            }

        current_warning = warnings[0]
        events = current_warning.get("events", [])

        # Count active alerts and find max severity
        active_alerts = []
        max_level = 1

        for event in events:
            event_level = event.get("max_level", 1)
            if event_level > 1:
                event_id = event.get("id")
                event_name = ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}")
                level_info = ALERT_LEVEL_MAP.get(event_level, ALERT_LEVEL_MAP[1])

                # Find description from reports
                description = None
                instruction = None
                for report in reports:
                    if report.get("event_id") == event_id:
                        levels = report.get("levels", [])
                        for level_data in levels:
                            if level_data.get("level") == event_level:
                                description = level_data.get("description")
                                instruction = level_data.get("instruction")
                                break

                active_alerts.append({
                    "event_name": event_name,
                    "level_name": level_info["name"],
                    "severity": level_info["severity"],
                    "description": description,
                })
                max_level = max(max_level, event_level)

        max_severity_info = ALERT_LEVEL_MAP.get(max_level, ALERT_LEVEL_MAP[1])
        alert_summary = ", ".join([f"{a['event_name']} ({a['level_name']})" for a in active_alerts])

        return {
            "active_alert_count": len(active_alerts),
            "max_severity": max_severity_info["severity"],
            "max_level": max_level,
            "alert_summary": alert_summary,
            "active_alerts": active_alerts,
            "area_id": self.coordinator.data.alerts.get("area_id"),
            "updated": self.coordinator.data.alerts.get("updated"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Fire events for alert changes
        self._fire_alert_events()
        super()._handle_coordinator_update()

    def _fire_alert_events(self) -> None:
        """Fire Home Assistant events for alert changes."""
        if not self.coordinator.data.alerts:
            return

        warnings = self.coordinator.data.alerts.get("warnings", [])
        if not warnings:
            return

        current_warning = warnings[0]
        events = current_warning.get("events", [])
        reports = self.coordinator.data.alerts.get("reports", [])

        # Build current alerts set
        current_alerts: set[tuple[int, int]] = set()
        for event in events:
            event_id = event.get("id")
            max_level = event.get("max_level", 1)
            if max_level > 1:
                current_alerts.add((event_id, max_level))

        # Find new alerts
        new_alerts = current_alerts - self._previous_alerts
        for event_id, level in new_alerts:
            event_name = ALERT_EVENT_MAP.get(event_id, f"unknown_{event_id}")
            level_info = ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])

            # Find description
            description = None
            for report in reports:
                if report.get("event_id") == event_id:
                    levels = report.get("levels", [])
                    for level_data in levels:
                        if level_data.get("level") == level:
                            description = level_data.get("description")
                            break

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
        updated_alerts = current_alerts & self._previous_alerts
        for event_id, level in updated_alerts:
            # Check if level changed
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
        cleared_alerts = self._previous_alerts - current_alerts
        for event_id, level in cleared_alerts:
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


class SMNEventAlertSensor(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], BinarySensorEntity):
    """Binary sensor for specific SMN weather alert type."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
        event_id: int,
        event_name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._event_id = event_id
        self._event_name = event_name
        self._attr_unique_id = f"{config_entry.entry_id}_alert_{event_name}"
        self._attr_translation_key = f"alert_{event_name}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def icon(self) -> str:
        """Return the icon for this alert type."""
        return ALERT_EVENT_ICONS.get(self._event_name, "mdi:alert")

    @property
    def is_on(self) -> bool:
        """Return True if this alert type is active."""
        if not self.coordinator.data.alerts:
            return False

        warnings = self.coordinator.data.alerts.get("warnings", [])
        if not warnings:
            return False

        current_warning = warnings[0]
        events = current_warning.get("events", [])

        for event in events:
            if event.get("id") == self._event_id and event.get("max_level", 1) > 1:
                return True

        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.is_on:
            return {"level": 1, "severity": "info"}

        warnings = self.coordinator.data.alerts.get("warnings", [])
        reports = self.coordinator.data.alerts.get("reports", [])
        current_warning = warnings[0]
        events = current_warning.get("events", [])

        for event in events:
            if event.get("id") == self._event_id:
                level = event.get("max_level", 1)
                level_info = ALERT_LEVEL_MAP.get(level, ALERT_LEVEL_MAP[1])

                # Find description from reports
                description = None
                instruction = None
                for report in reports:
                    if report.get("event_id") == self._event_id:
                        levels = report.get("levels", [])
                        for level_data in levels:
                            if level_data.get("level") == level:
                                description = level_data.get("description")
                                instruction = level_data.get("instruction")
                                break

                return {
                    "event_id": self._event_id,
                    "event_name": self._event_name,
                    "level": level,
                    "level_name": level_info["name"],
                    "color": level_info["color"],
                    "severity": level_info["severity"],
                    "date": current_warning.get("date"),
                    "description": description,
                    "instruction": instruction,
                }

        return {"level": 1, "severity": "info"}


class SMNShortTermAlertSensor(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], BinarySensorEntity):
    """Binary sensor for SMN short-term severe weather alerts."""

    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True
    _attr_translation_key = "short_term_alert"
    _attr_icon = "mdi:alert-circle"

    def __init__(
        self,
        coordinator: ArgentinaSMNDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_shortterm_alert"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get(CONF_NAME, "SMN Weather"),
            manufacturer="Servicio Meteorológico Nacional",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        """Return True if there are active short-term alerts."""
        return bool(self.coordinator.data.shortterm_alerts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        if not self.coordinator.data.shortterm_alerts:
            return {"alert_count": 0}

        alerts = self.coordinator.data.shortterm_alerts

        return {
            "alert_count": len(alerts),
            "alerts": [
                {
                    "title": alert.get("title"),
                    "date": alert.get("date"),
                    "end_date": alert.get("end_date"),
                    "severity": alert.get("severity"),
                    "zones": alert.get("zones"),
                    "instructions": alert.get("instructions"),
                    "region": alert.get("region"),
                }
                for alert in alerts
            ],
        }


_HAIL_KEYWORD = "granizo"
_HAIL_TORMENTA_EVENT_ID = 41  # ALERT_EVENT_MAP[41] == "tormenta"


def _mentions_hail(alert: dict[str, Any]) -> bool:
    text = f"{alert.get('title') or ''} {alert.get('description') or ''}"
    return _HAIL_KEYWORD in text.lower()


def _zone_hail_mentions(alerts_data: dict[str, Any]) -> list[str]:
    """Hail mentions in today's active "tormenta" alert level description.

    That text is SMN's generic description for the alert *level* of the
    whole zone (e.g. "...ocasional granizo..." appears in many yellow storm
    alerts), not a forecast of hail at a specific point — so it's reported
    as context, not used to turn the sensor on.
    """
    warnings = (alerts_data or {}).get("warnings") or []
    reports = (alerts_data or {}).get("reports") or []
    if not warnings:
        return []
    active_level = next(
        (
            event.get("max_level", 1)
            for event in warnings[0].get("events", [])
            if event.get("id") == _HAIL_TORMENTA_EVENT_ID and event.get("max_level", 1) > 1
        ),
        None,
    )
    if not active_level:
        return []
    mentions = []
    for report in reports:
        if report.get("event_id") != _HAIL_TORMENTA_EVENT_ID:
            continue
        for level_data in report.get("levels", []):
            description = level_data.get("description") or ""
            if level_data.get("level") == active_level and _HAIL_KEYWORD in description.lower():
                mentions.append(description.strip())
    return mentions


def _distance_to_alert_km(
    latitude: float, longitude: float, alert: dict[str, Any]
) -> tuple[float, float] | None:
    """(distance in km, bearing in degrees) from a point to an alert's polygon.

    0 km when the point is inside it. Uses a local equirectangular
    projection around the point — accurate to well under 1% at the few
    hundred km this is used for.
    """
    rings = ((alert.get("geometry") or {}).get("coordinates")) or []
    km_per_deg_lat = 110.574
    km_per_deg_lon = 111.320 * math.cos(math.radians(latitude))
    best: tuple[float, float] | None = None
    for ring in rings:
        points = [((lon - longitude) * km_per_deg_lon, (lat - latitude) * km_per_deg_lat) for lon, lat in ring]
        if len(points) < 3:
            continue
        inside = False
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
            if (y0 > 0) != (y1 > 0) and 0 < x0 + (0 - y0) * (x1 - x0) / (y1 - y0):
                inside = not inside
            dx, dy = x1 - x0, y1 - y0
            length_sq = dx * dx + dy * dy
            t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(x0 * dx + y0 * dy) / length_sq))
            nx, ny = x0 + t * dx, y0 + t * dy
            distance = math.hypot(nx, ny)
            if best is None or distance < best[0]:
                best = (distance, math.degrees(math.atan2(nx, ny)) % 360)
        if inside:
            return 0.0, 0.0
    return best


class _SMNHailBase(CoordinatorEntity[ArgentinaSMNDataUpdateCoordinator], BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-hail"

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


class SMNHailAlertSensor(_SMNHailBase):
    """On when a short-term warning covering the exact location mentions hail.

    SMN's warning API has no dedicated hail event. The precise signal is a
    warning/shortterm alert for this location (SMN's own point-in-polygon
    test against the configured coordinates) whose text says "granizo".
    The zone-wide "tormenta" level description is exposed as context only
    (see _zone_hail_mentions for why it isn't precise enough).
    """

    _attr_translation_key = "hail_alert"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_hail_alert")

    def _matching(self) -> list[dict[str, Any]]:
        return [a for a in self.coordinator.data.shortterm_alerts or [] if _mentions_hail(a)]

    @property
    def is_on(self) -> bool:
        return bool(self._matching())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        matching = self._matching()
        return {
            "alerts": [
                {"title": (a.get("title") or "").strip(), "end_date": a.get("end_date")}
                for a in matching
            ],
            "zone_alert_mentions": _zone_hail_mentions(self.coordinator.data.alerts),
        }


class SMNNearbyHailSensor(_SMNHailBase):
    """On when a short-term warning that mentions hail is within the configured radius.

    Uses the nationwide warning/shortterm list (with its polygons) to find
    the closest hail warning to the configured location — an early heads-up
    before a storm reaches the exact point "Alerta por granizo" checks.
    """

    _attr_translation_key = "nearby_hail"

    def __init__(self, coordinator, config_entry) -> None:
        super().__init__(coordinator, config_entry, "_nearby_hail")

    @property
    def _radius_km(self) -> float:
        return float(self._config_entry.options.get(CONF_HAIL_RADIUS_KM, DEFAULT_HAIL_RADIUS_KM))

    def _nearest(self) -> tuple[float, float, dict[str, Any]] | None:
        latitude = self._config_entry.data[CONF_LATITUDE]
        longitude = self._config_entry.data[CONF_LONGITUDE]
        nearest = None
        for alert in self.coordinator.data.nationwide_shortterm_alerts or []:
            if not _mentions_hail(alert):
                continue
            result = _distance_to_alert_km(latitude, longitude, alert)
            if result and (nearest is None or result[0] < nearest[0]):
                nearest = (result[0], result[1], alert)
        return nearest

    @property
    def is_on(self) -> bool:
        nearest = self._nearest()
        return nearest is not None and nearest[0] <= self._radius_km

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"radius_km": self._radius_km}
        nearest = self._nearest()
        if nearest is None:
            return attrs
        distance, bearing, alert = nearest
        attrs.update(
            {
                "distance_km": round(distance, 1),
                "direction": None if distance == 0 else wind_cardinal(bearing),
                "title": (alert.get("title") or "").strip(),
                "end_date": alert.get("end_date"),
            }
        )
        return attrs
