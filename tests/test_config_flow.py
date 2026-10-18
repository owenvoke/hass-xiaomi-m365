"""Tests for the Xiaomi M365 Scooter config flow."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_m365.const import (
    CONF_ADDRESS,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL,
    DOMAIN,
)
from custom_components.xiaomi_m365.exceptions import M365AuthError
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType

from . import ADDRESS, NAME, TOKEN, service_info

pytestmark = pytest.mark.usefixtures("mock_setup_entry")

DISCOVERED = "custom_components.xiaomi_m365.config_flow.async_discovered_service_info"


async def _pair(hass: HomeAssistant, flow_id: str) -> FlowResult:
    """Submit a pairing step, let pairing finish, and return what follows."""
    result = await hass.config_entries.flow.async_configure(flow_id, {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["progress_action"] == "pairing"

    await hass.async_block_till_done()
    return await hass.config_entries.flow.async_configure(flow_id)


async def _start_user(hass: HomeAssistant) -> FlowResult:
    """Pick the scooter from the user step and return the pairing step."""
    with patch(DISCOVERED, return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["step_id"] == "pair"
    return result


@pytest.mark.usefixtures("mock_pair")
async def test_user_flow(hass: HomeAssistant) -> None:
    """A scooter picked from the list is paired and added."""
    result = await _pair(hass, (await _start_user(hass))["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_TOKEN: TOKEN.hex()}
    assert result["result"].unique_id == ADDRESS


async def test_user_flow_without_pairing(
    hass: HomeAssistant, mock_pair: AsyncMock
) -> None:
    """A scooter that answers unauthenticated is added with no token."""
    mock_pair.return_value = None
    result = await _pair(hass, (await _start_user(hass))["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: ADDRESS}


async def test_user_flow_pairing_fails_then_recovers(
    hass: HomeAssistant, mock_pair: AsyncMock
) -> None:
    """A failed pairing returns to the form with an error, and can be retried."""
    flow_id = (await _start_user(hass))["flow_id"]

    mock_pair.side_effect = M365AuthError("no button press")
    result = await _pair(hass, flow_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair"
    assert result["errors"] == {"base": "pairing_failed"}

    mock_pair.side_effect = None
    result = await _pair(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_offers_everything_when_nothing_matches(
    hass: HomeAssistant,
) -> None:
    """A renamed scooter is still offered, among every nearby device."""
    renamed = service_info(name="MyScooter")
    with patch(DISCOVERED, return_value=[renamed]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["step_id"] == "user"
    schema = result["data_schema"].schema[CONF_ADDRESS]
    assert schema.container == {ADDRESS: f"MyScooter ({ADDRESS})"}


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """With nothing nearby there is nothing to offer."""
    with patch(DISCOVERED, return_value=[]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_hides_configured_scooters(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A scooter already added is not offered again."""
    mock_config_entry.add_to_hass(hass)
    with patch(DISCOVERED, return_value=[service_info()]):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


@pytest.mark.usefixtures("mock_pair")
async def test_bluetooth_flow(hass: HomeAssistant) -> None:
    """A discovered scooter is confirmed, paired and added."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "pair"

    result = await _pair(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME


async def test_bluetooth_flow_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """A scooter already added is not discovered again."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_pair: AsyncMock
) -> None:
    """Pairing again replaces the token the scooter stopped honouring."""
    mock_config_entry.add_to_hass(hass)
    mock_pair.return_value = b"\xff" * 12

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"] == {"name": "Scooter"}

    result = await _pair(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKEN] == "ff" * 12


async def test_reauth_flow_drops_token_no_longer_needed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_pair: AsyncMock
) -> None:
    """A scooter that now answers unauthenticated loses its stale token."""
    mock_config_entry.add_to_hass(hass)
    mock_pair.return_value = None

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await _pair(hass, result["flow_id"])

    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {CONF_ADDRESS: ADDRESS}


async def test_reconfigure_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_pair: AsyncMock
) -> None:
    """Re-pairing on request keeps the entry, and reloads it either way."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    mock_pair.side_effect = M365AuthError("no button press")
    result = await _pair(hass, result["flow_id"])
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == {"base": "pairing_failed"}
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    mock_pair.side_effect = None
    mock_pair.return_value = b"\xff" * 12
    result = await _pair(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_TOKEN] == "ff" * 12

    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_options_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The poll interval is stored as whole seconds."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPDATE_INTERVAL: 30.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_UPDATE_INTERVAL: 30}
    assert isinstance(mock_config_entry.options[CONF_UPDATE_INTERVAL], int)
