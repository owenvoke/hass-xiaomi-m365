"""Base entity for the Xiaomi M365 Scooter integration."""

from __future__ import annotations

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import M365DataCoordinator


class M365Entity(CoordinatorEntity[M365DataCoordinator]):
    """Common device wiring and availability for every M365 entity.

    State restoration happens once, in the coordinator, rather than per
    entity — see ``M365DataCoordinator.async_load_stored_data``.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: M365DataCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._address = coordinator.address

        # Keyed on the Bluetooth address rather than the config entry id, so
        # removing and re-adding the scooter keeps the entity's history.
        self._attr_unique_id = f"{self._address}_{key}"

        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=coordinator.name,
            serial_number=coordinator.data.get("serial_number"),
            sw_version=coordinator.data.get("firmware_version"),
        )

    @property
    def available(self) -> bool:
        """Return True once the scooter has ever reported state.

        A scooter spends nearly all its time switched off, so tying
        availability to being reachable right now would leave every entity
        unavailable except while riding. Once it has been seen — live or
        restored from the cache — the last known values stay on show.
        """
        if self.coordinator.data.get("last_seen"):
            return True

        # Never seen, so fall back to whether it is in range at all.
        return async_last_service_info(self.hass, self._address) is not None
