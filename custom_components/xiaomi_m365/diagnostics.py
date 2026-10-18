"""Diagnostics support for the Xiaomi M365 Scooter integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_TOKEN
from .types import M365ConfigEntry

# The token is the pairing secret itself, and diagnostics end up attached to
# public issues.
TO_REDACT = {CONF_ADDRESS, CONF_TOKEN, "serial_number"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: M365ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "version": f"{entry.version}.{entry.minor_version}",
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval": str(coordinator.update_interval),
            "connected": coordinator.transport.is_connected,
            "data": async_redact_data(dict(coordinator.data), TO_REDACT),
        },
    }
