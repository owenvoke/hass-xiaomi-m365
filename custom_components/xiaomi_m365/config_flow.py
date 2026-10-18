"""Config flow for the Xiaomi M365 Scooter integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import (
    CONF_ADDRESS,
    CONF_TOKEN,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    LOCAL_NAME_PREFIXES,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .exceptions import M365AuthError
from .transport import M365Transport

_LOGGER = logging.getLogger(__name__)


def _is_scooter(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True when an advertisement looks like an M365.

    The advertised name is all there is to go on, and it is what the
    manifest's discovery matchers use too. The UART service is not in the
    advertisement — it only appears once connected and the service list has
    been read — so requiring it here would hide scooters that Home Assistant
    can see perfectly well.
    """
    return (service_info.name or "").startswith(LOCAL_NAME_PREFIXES)


def _name(service_info: BluetoothServiceInfoBleak) -> str | None:
    """Return the advertised name, or None when there was not one.

    Home Assistant stands the address in for the name when an advertisement
    carries none, which is not a name worth showing to anyone.
    """
    name = service_info.name
    return None if not name or name == service_info.address else name


def _title(service_info: BluetoothServiceInfoBleak) -> str:
    """Return a display name for a discovered scooter."""
    return _name(service_info) or f"Xiaomi M365 ({service_info.address})"


async def _async_pair(hass: HomeAssistant, address: str) -> bytes | None:
    """Return a token for ``address``, or None when it needs none.

    A scooter that answers a plain read is not gating the UART behind Xiaomi's
    authentication, and pairing it would gain nothing while costing the user a
    button press — and, since the scooter keeps only one binding, evicting
    whatever app they last paired.
    """
    transport = M365Transport(hass, address)

    try:
        if await transport.async_probe():
            _LOGGER.debug("%s: answers unauthenticated, no pairing needed", address)
            return None

        _LOGGER.debug("%s: no answer unauthenticated, pairing", address)
        return await transport.async_register()
    finally:
        await transport.async_disconnect()


def _label(service_info: BluetoothServiceInfoBleak) -> str:
    """Return a row for the device picker.

    The address is spelled out alongside the name, because the fallback list in
    ``async_step_user`` can hold devices that have nothing to do with a
    scooter — and a renamed scooter is picked out by its address.
    """
    if (name := _name(service_info)) is None:
        return service_info.address
    return f"{name} ({service_info.address})"


class M365ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Xiaomi M365 Scooter."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}
        self._address: str | None = None
        self._title: str | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a scooter found by Home Assistant's Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm adding a discovered scooter."""
        assert self._discovery is not None

        if user_input is not None:
            self._address = self._discovery.address
            self._title = _title(self._discovery)
            return await self.async_step_pair()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(self._discovery)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow started from the UI."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            service_info = self._discovered.get(address)
            self._address = address
            self._title = (
                _title(service_info) if service_info else f"Xiaomi M365 ({address})"
            )

            return await self.async_step_pair()

        configured = self._async_current_ids()
        # Connectable only: the integration polls over a real connection, so a
        # scooter seen through a passive proxy is no use here.
        nearby = [
            service_info
            for service_info in async_discovered_service_info(
                self.hass, connectable=True
            )
            if service_info.address not in configured
        ]
        scooters = [
            service_info for service_info in nearby if _is_scooter(service_info)
        ]

        # A renamed scooter — custom firmware, or a name changed over BLE —
        # matches no prefix, and automatic discovery cannot see it either.
        # Offering everything nearby beats insisting there is nothing to add;
        # picking something that is not a scooter costs nothing, as it simply
        # never answers a register read.
        if not scooters and nearby:
            _LOGGER.debug(
                "No advertisement matched %s; offering all %d nearby device(s)",
                LOCAL_NAME_PREFIXES,
                len(nearby),
            )

        self._discovered = {
            service_info.address: service_info for service_info in scooters or nearby
        }

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(
                    dict(
                        sorted(
                            (
                                (address, _label(service_info))
                                for address, service_info in self._discovered.items()
                            ),
                            key=lambda row: row[1],
                        )
                    )
                )
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair with the scooter, if its firmware insists on being paired.

        Older firmware answers a plain read and needs nothing here, so this
        asks the scooter before asking the person: only if the read goes
        unanswered does it come back for a button press.
        """
        assert self._address is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                token = await _async_pair(self.hass, self._address)
            except M365AuthError as err:
                _LOGGER.debug("%s: pairing failed: %s", self._address, err)
                errors["base"] = "pairing_failed"
            else:
                data = {CONF_ADDRESS: self._address}
                if token is not None:
                    data[CONF_TOKEN] = token.hex()

                return self.async_create_entry(title=self._title or "", data=data)

        return self.async_show_form(
            step_id="pair",
            errors=errors,
            description_placeholders={"name": self._title or self._address},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token the scooter has stopped honouring."""
        self._address = entry_data[CONF_ADDRESS]
        self._title = self.context.get("title_placeholders", {}).get("name")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again, keeping the existing entry and its history."""
        assert self._address is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                token = await _async_pair(self.hass, self._address)
            except M365AuthError as err:
                _LOGGER.debug("%s: re-pairing failed: %s", self._address, err)
                errors["base"] = "pairing_failed"
            else:
                entry = self._get_reauth_entry()
                data = dict(entry.data)
                if token is None:
                    # The scooter now answers without pairing; keeping the old
                    # token would only fail the next login and loop back here.
                    data.pop(CONF_TOKEN, None)
                else:
                    data[CONF_TOKEN] = token.hex()

                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={"name": self._title or self._address},
        )

    @staticmethod
    def async_get_options_flow(entry: ConfigEntry) -> M365OptionsFlow:
        """Return the options flow."""
        return M365OptionsFlow()


class M365OptionsFlow(OptionsFlowWithReload):
    """Handle the Xiaomi M365 Scooter options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the poll interval."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=current
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_UPDATE_INTERVAL,
                        max=MAX_UPDATE_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
