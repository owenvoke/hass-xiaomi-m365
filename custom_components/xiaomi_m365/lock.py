"""Lock platform for the Xiaomi M365 Scooter."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import M365DataCoordinator
from .entity import M365Entity
from .types import M365ConfigEntry

LOCK_DESCRIPTION = LockEntityDescription(
    key="motor_lock",
    translation_key="motor_lock",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: M365ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the scooter's motor lock."""
    async_add_entities([M365ScooterLock(entry.runtime_data, LOCK_DESCRIPTION)])


class M365ScooterLock(M365Entity, LockEntity):
    """The scooter's motor lock."""

    entity_description: LockEntityDescription

    def __init__(
        self,
        coordinator: M365DataCoordinator,
        description: LockEntityDescription,
    ) -> None:
        """Initialize the lock."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_locked(self) -> bool | None:
        """Return True while the motor lock is engaged."""
        return self.coordinator.data.get("is_locked")

    @property
    def available(self) -> bool:
        """Return True once the scooter has reported its lock state."""
        return super().available and self.is_locked is not None

    async def async_lock(self, **kwargs: Any) -> None:
        """Engage the motor lock."""
        await self.coordinator.async_set_lock(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Release the motor lock."""
        await self.coordinator.async_set_lock(False)
