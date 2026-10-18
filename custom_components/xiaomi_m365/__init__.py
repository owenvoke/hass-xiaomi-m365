"""The Xiaomi M365 Scooter integration."""

from __future__ import annotations

import logging

from homeassistant.components.bluetooth import async_rediscover_address
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_ADDRESS, CONF_TOKEN, DOMAIN
from .coordinator import M365ConfigEntry, M365DataCoordinator, async_remove_stored_data
from .transport import M365Transport

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: M365ConfigEntry) -> bool:
    """Set up a Xiaomi M365 Scooter from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    # Absent for scooters whose firmware relays the UART without pairing.
    stored_token: str | None = entry.data.get(CONF_TOKEN)
    token = bytes.fromhex(stored_token) if stored_token else None

    transport = M365Transport(hass, address, token)
    coordinator = M365DataCoordinator(hass, entry, transport)

    # Deliberately not a blocking first refresh. A scooter is switched off far
    # more often than it is on, and failing setup for that would leave the
    # entry retrying instead of showing the values it already knows. Entities
    # come up from the restore cache (or as unknown on a fresh install) and
    # fill in the next time the scooter is awake.
    await coordinator.async_load_stored_data()
    coordinator.async_set_updated_data(coordinator.data)

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: M365ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    await entry.runtime_data.async_shutdown()

    # Let the scooter be offered as a discovery again if it is re-added.
    async_rediscover_address(hass, entry.data[CONF_ADDRESS])

    return True


async def async_remove_entry(hass: HomeAssistant, entry: M365ConfigEntry) -> None:
    """Drop the restore cache when the entry is deleted for good."""
    await async_remove_stored_data(hass, entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: M365ConfigEntry) -> bool:
    """Migrate an entry created by an older version."""
    if entry.version > 1:
        # Written by a newer major version — refuse rather than guess.
        return False

    if entry.minor_version < 2:
        _async_migrate_unique_ids(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)

    _LOGGER.debug(
        "Migrated entry %s to version %s.%s",
        entry.entry_id,
        entry.version,
        entry.minor_version,
    )
    return True


@callback
def _async_migrate_unique_ids(hass: HomeAssistant, entry: M365ConfigEntry) -> None:
    """Re-key entity unique ids from the config entry id to the BLE address.

    Entities used to be identified by ``{entry_id}_{key}``, which meant
    removing and re-adding the scooter lost their history. Anything already in
    the registry under the old scheme is renamed in place.
    """
    address = entry.data[CONF_ADDRESS]
    registry = er.async_get(hass)

    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        old_prefix = f"{entry.entry_id}_"
        if not registry_entry.unique_id.startswith(old_prefix):
            continue

        # The per-entity keys themselves are unchanged; only the prefix moves.
        key = registry_entry.unique_id.removeprefix(old_prefix)
        new_unique_id = f"{address}_{key}"

        if (
            registry.async_get_entity_id(registry_entry.domain, DOMAIN, new_unique_id)
            is not None
        ):
            # Something already owns the new id; leave the stale row for the
            # user to delete rather than colliding.
            continue

        registry.async_update_entity(
            registry_entry.entity_id, new_unique_id=new_unique_id
        )
        _LOGGER.debug(
            "Migrated %s unique id to %s", registry_entry.entity_id, new_unique_id
        )
