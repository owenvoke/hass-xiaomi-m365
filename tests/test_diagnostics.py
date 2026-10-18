"""Tests for the Xiaomi M365 Scooter diagnostics."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_bluetooth")


async def test_diagnostics_redact_secrets(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    mock_config_entry: MockConfigEntry,
    mock_transport: MagicMock,
) -> None:
    """The pairing token, address and serial never appear in diagnostics."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert diagnostics["entry"]["data"] == {"address": REDACTED, "token": REDACTED}
    assert diagnostics["coordinator"]["data"]["battery"] == 47
    assert mock_config_entry.data["token"] not in str(diagnostics)
