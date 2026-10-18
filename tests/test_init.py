"""Tests for setting up the Xiaomi M365 Scooter integration."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_m365.const import DOMAIN
from custom_components.xiaomi_m365.exceptions import (
    M365AuthError,
    M365ConnectionError,
    M365DeviceAsleep,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import ADDRESS

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

BATTERY = "sensor.scooter_battery"
ODOMETER = "sensor.scooter_odometer"
SPEED = "sensor.scooter_speed"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up ``entry``."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Run one poll of the scooter."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_setup_without_scooter(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """Setup succeeds with the scooter off, and polls nothing up front."""
    await _setup(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    mock_transport.async_request.assert_not_called()
    # Never seen and not in range.
    assert hass.states.get(BATTERY).state == STATE_UNAVAILABLE


async def test_poll_updates_entities(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """A poll fills in the sensors and the lock."""
    await _setup(hass, mock_config_entry)
    await _poll(hass, mock_config_entry)

    assert hass.states.get(BATTERY).state == "47"
    assert hass.states.get(SPEED).state == "12.3"
    assert hass.states.get(ODOMETER).state == "123.45"
    assert hass.states.get("lock.scooter_motor_lock").state == "locked"
    # Range was not answered, so it stays unavailable rather than empty.
    assert hass.states.get("sensor.scooter_range_remaining").state == (
        STATE_UNAVAILABLE
    )


async def test_restores_lasting_values_only(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_config_entry: MockConfigEntry,
    mock_transport: MagicMock,
) -> None:
    """Battery and odometer survive a restart; a momentary speed does not."""
    await _setup(hass, mock_config_entry)
    await _poll(hass, mock_config_entry)
    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    stored = hass_storage[f"{DOMAIN}.{mock_config_entry.entry_id}"]["data"]
    assert stored["battery"] == 47
    assert "speed" not in stored

    mock_transport.async_request.side_effect = M365DeviceAsleep("off")
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(BATTERY).state == "47"
    assert hass.states.get(SPEED).state in (STATE_UNAVAILABLE, STATE_UNKNOWN)


async def test_asleep_keeps_last_values(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """A switched-off scooter is not a failure, and keeps its last values."""
    await _setup(hass, mock_config_entry)
    await _poll(hass, mock_config_entry)

    mock_transport.async_request.side_effect = M365DeviceAsleep("off")
    await _poll(hass, mock_config_entry)

    assert mock_config_entry.runtime_data.last_update_success
    assert hass.states.get(BATTERY).state == "47"


async def test_connection_error_fails_update(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """A real fault is reported as a failed update."""
    await _setup(hass, mock_config_entry)

    mock_transport.async_request.side_effect = M365ConnectionError("dropped")
    await _poll(hass, mock_config_entry)

    assert not mock_config_entry.runtime_data.last_update_success


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """A token the scooter no longer honours asks to pair again."""
    await _setup(hass, mock_config_entry)

    mock_transport.async_request.side_effect = M365AuthError("rejected")
    await _poll(hass, mock_config_entry)

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_unload_disconnects(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """Unloading closes the BLE connection."""
    await _setup(hass, mock_config_entry)

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_transport.async_disconnect.assert_awaited()


async def test_migrate_unique_ids(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    mock_transport: MagicMock,
) -> None:
    """Entities keyed on the entry id move to the Bluetooth address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Scooter",
        version=1,
        minor_version=1,
        data={"address": ADDRESS},
    )
    entry.add_to_hass(hass)
    old = entity_registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_battery", config_entry=entry
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert entity_registry.async_get(old.entity_id).unique_id == f"{ADDRESS}_battery"


async def test_migrate_refuses_newer_version(
    hass: HomeAssistant, mock_transport: MagicMock
) -> None:
    """An entry from a newer major version is left alone."""
    entry = MockConfigEntry(domain=DOMAIN, version=2, data={"address": ADDRESS})
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
