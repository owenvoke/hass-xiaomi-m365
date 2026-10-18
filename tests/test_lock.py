"""Tests for the Xiaomi M365 Scooter motor lock."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_m365.const import DOMAIN
from custom_components.xiaomi_m365.exceptions import (
    M365ConnectionError,
    M365DeviceAsleep,
)
from custom_components.xiaomi_m365.protocol import (
    LOCK_WRITE_PAYLOAD,
    REG_LOCK,
    REG_UNLOCK,
    Device,
)
from homeassistant.components.lock import (
    DOMAIN as LOCK_DOMAIN,
    SERVICE_LOCK,
    SERVICE_UNLOCK,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

pytestmark = pytest.mark.usefixtures("enable_bluetooth")

LOCK = "lock.scooter_motor_lock"


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_transport: MagicMock
) -> None:
    """Set up the entry and poll once, so the lock state is known."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


@pytest.mark.usefixtures("setup_entry")
@pytest.mark.parametrize(
    ("service", "register"),
    [(SERVICE_LOCK, REG_LOCK), (SERVICE_UNLOCK, REG_UNLOCK)],
)
async def test_lock_and_unlock(
    hass: HomeAssistant, mock_transport: MagicMock, service: str, register: int
) -> None:
    """Locking and unlocking write to their own registers."""
    await hass.services.async_call(
        LOCK_DOMAIN, service, {ATTR_ENTITY_ID: LOCK}, blocking=True
    )

    mock_transport.async_write.assert_awaited_once_with(
        Device.ESC, register, LOCK_WRITE_PAYLOAD
    )


@pytest.mark.usefixtures("setup_entry")
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (M365DeviceAsleep("off"), "scooter_asleep"),
        (M365ConnectionError("dropped"), "lock_failed"),
    ],
)
async def test_lock_errors_are_translated(
    hass: HomeAssistant,
    mock_transport: MagicMock,
    error: Exception,
    translation_key: str,
) -> None:
    """A failed lock reports why, in a translatable way."""
    mock_transport.async_write.side_effect = error

    with pytest.raises(HomeAssistantError) as exc_info:
        await hass.services.async_call(
            LOCK_DOMAIN, SERVICE_UNLOCK, {ATTR_ENTITY_ID: LOCK}, blocking=True
        )

    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == translation_key
