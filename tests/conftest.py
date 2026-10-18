"""Fixtures for the Xiaomi M365 Scooter tests."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_m365.const import CONF_ADDRESS, CONF_TOKEN, DOMAIN
from custom_components.xiaomi_m365.protocol import REPLY_FOR, Frame

from . import ADDRESS, TOKEN, bms_info_frame, general_info_frame, lock_state_frame


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return an entry for a paired scooter."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Scooter",
        version=1,
        minor_version=2,
        data={CONF_ADDRESS: ADDRESS, CONF_TOKEN: TOKEN.hex()},
    )


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stop an entry from being set up, or torn down, for real."""
    with (
        patch(
            "custom_components.xiaomi_m365.async_setup_entry", return_value=True
        ) as mock,
        patch("custom_components.xiaomi_m365.async_unload_entry", return_value=True),
    ):
        yield mock


@pytest.fixture
def mock_pair() -> Generator[AsyncMock]:
    """Stand in for pairing, which needs a scooter and a button press."""
    with patch(
        "custom_components.xiaomi_m365.config_flow._async_pair", return_value=TOKEN
    ) as mock:
        yield mock


@pytest.fixture
def replies() -> dict[tuple[int, int], Frame]:
    """Return the frames the mock scooter answers with, by device and register."""
    request_for = {reply: request for request, reply in REPLY_FOR.items()}
    return {
        (request_for[frame.device], frame.register): frame
        for frame in (general_info_frame(), lock_state_frame(True), bms_info_frame())
    }


@pytest.fixture
def mock_transport(replies: dict[tuple[int, int], Frame]) -> Generator[MagicMock]:
    """Replace the BLE transport with one answering from ``replies``."""

    async def request(device: int, register: int, length: int) -> Frame | None:
        return replies.get((device, register))

    with patch("custom_components.xiaomi_m365.M365Transport", autospec=True) as cls:
        transport = cls.return_value
        transport.address = ADDRESS
        transport.is_connected = True
        transport.async_request.side_effect = request
        yield transport
