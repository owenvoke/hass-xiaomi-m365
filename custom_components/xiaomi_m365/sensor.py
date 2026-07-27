"""Sensor platform for the Xiaomi M365 Scooter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import M365DataCoordinator
from .entity import M365Entity
from .types import M365ConfigEntry


@dataclass(frozen=True, kw_only=True)
class M365SensorEntityDescription(SensorEntityDescription):
    """Describes an M365 scooter sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


SENSOR_TYPES: tuple[M365SensorEntityDescription, ...] = (
    M365SensorEntityDescription(
        key="battery",
        translation_key="battery",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("battery"),
    ),
    M365SensorEntityDescription(
        key="speed",
        translation_key="speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("speed"),
    ),
    M365SensorEntityDescription(
        key="average_speed",
        translation_key="average_speed",
        native_unit_of_measurement=UnitOfSpeed.KILOMETERS_PER_HOUR,
        device_class=SensorDeviceClass.SPEED,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("average_speed"),
    ),
    M365SensorEntityDescription(
        key="odometer",
        translation_key="odometer",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda data: data.get("odometer"),
    ),
    M365SensorEntityDescription(
        key="trip_distance",
        translation_key="trip_distance",
        native_unit_of_measurement=UnitOfLength.METERS,
        device_class=SensorDeviceClass.DISTANCE,
        # Resets to zero at the start of each ride, so it is neither a total
        # nor monotonic — a plain measurement is the honest state class.
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("trip_distance"),
    ),
    M365SensorEntityDescription(
        key="trip_time",
        translation_key="trip_time",
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("trip_time"),
    ),
    M365SensorEntityDescription(
        key="range_remaining",
        translation_key="range_remaining",
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda data: data.get("range_remaining"),
    ),
    M365SensorEntityDescription(
        key="frame_temperature",
        translation_key="frame_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.get("frame_temperature"),
    ),
    M365SensorEntityDescription(
        key="battery_voltage",
        translation_key="battery_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("battery_voltage"),
    ),
    M365SensorEntityDescription(
        key="battery_current",
        translation_key="battery_current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("battery_current"),
    ),
    M365SensorEntityDescription(
        key="battery_temperature",
        translation_key="battery_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("battery_temperature"),
    ),
    M365SensorEntityDescription(
        key="battery_capacity",
        translation_key="battery_capacity",
        # No sensor device class covers charge, so this carries a bare unit.
        native_unit_of_measurement="mAh",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.get("battery_capacity"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: M365ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the scooter's sensors."""
    coordinator = entry.runtime_data

    async_add_entities(
        M365ScooterSensor(coordinator, description) for description in SENSOR_TYPES
    )


class M365ScooterSensor(M365Entity, SensorEntity):
    """A single value read from the scooter."""

    entity_description: M365SensorEntityDescription

    def __init__(
        self,
        coordinator: M365DataCoordinator,
        description: M365SensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """Return the value, or None while the scooter has not reported it."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        """Return True once this particular value has been reported.

        Not every register answers on every firmware revision, so a sensor
        stays unavailable until its own value has turned up at least once
        rather than showing an empty state forever.
        """
        return super().available and self.native_value is not None
