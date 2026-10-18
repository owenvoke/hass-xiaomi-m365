"""Config flow for the Xiaomi M365 Scooter integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
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


async def _async_pair_entry(hass: HomeAssistant, entry: ConfigEntry) -> bytes | None:
    """Pair the scooter behind an existing entry.

    The scooter takes one connection at a time, and a loaded entry holds it
    between polls, so the entry is unloaded for the duration. On success the
    flow reloads it with the new token; on failure, or if the flow is closed
    mid-pairing, it is brought back as it was.
    """
    await hass.config_entries.async_unload(entry.entry_id)

    try:
        return await _async_pair(hass, entry.data[CONF_ADDRESS])
    except BaseException:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        raise


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
        self._pair_step = "pair"
        self._pair_task: asyncio.Task[bytes | None] | None = None
        self._token: bytes | None = None
        self._errors: dict[str, str] = {}

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
        """Explain pairing before starting it.

        Older firmware answers a plain read and needs nothing here, so pairing
        asks the scooter before asking the person: only if the read goes
        unanswered does it wait for a button press.
        """
        return await self._async_step_confirm_pairing("pair", user_input)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle a token the scooter has stopped honouring."""
        self._address = entry_data[CONF_ADDRESS]
        self._title = self._get_reauth_entry().title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again, keeping the existing entry and its history."""
        return await self._async_step_confirm_pairing("reauth_confirm", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again on request, such as after using the Mi Home app."""
        entry = self._get_reconfigure_entry()
        self._address = entry.data[CONF_ADDRESS]
        self._title = entry.title
        return await self._async_step_confirm_pairing("reconfigure", user_input)

    async def _async_step_confirm_pairing(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Show a step's pairing instructions, then start pairing on submit.

        A failed attempt comes back here with the error, so the step is
        remembered for the progress step to return to.
        """
        assert self._address is not None

        if user_input is not None:
            self._pair_step = step_id
            self._errors = {}
            return await self.async_step_pairing()

        errors, self._errors = self._errors, {}
        return self.async_show_form(
            step_id=step_id,
            errors=errors,
            description_placeholders={"name": self._title or self._address},
        )

    async def async_step_pairing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair in the background, showing progress while it runs.

        Pairing can take a couple of minutes over several attempts while
        someone finds the power button, which is too long to hold a form
        submission open.
        """
        assert self._address is not None

        if self._pair_task is None:
            self._pair_task = self.hass.async_create_task(
                self._async_pair(), eager_start=False
            )

        if not self._pair_task.done():
            return self.async_show_progress(
                step_id="pairing",
                progress_action="pairing",
                progress_task=self._pair_task,
                description_placeholders={"name": self._title or self._address},
            )

        task, self._pair_task = self._pair_task, None
        try:
            self._token = task.result()
        except M365AuthError as err:
            _LOGGER.debug("%s: pairing failed: %s", self._address, err)
            self._errors = {"base": "pairing_failed"}
            return self.async_show_progress_done(next_step_id=self._pair_step)

        return self.async_show_progress_done(next_step_id="pair_finish")

    async def _async_pair(self) -> bytes | None:
        """Pair the scooter this flow is for."""
        assert self._address is not None

        if self.source == SOURCE_REAUTH:
            return await _async_pair_entry(self.hass, self._get_reauth_entry())
        if self.source == SOURCE_RECONFIGURE:
            return await _async_pair_entry(self.hass, self._get_reconfigure_entry())
        return await _async_pair(self.hass, self._address)

    async def async_step_pair_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry, or update the one being paired again."""
        assert self._address is not None

        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            data = dict(entry.data)
            if self._token is None:
                # The scooter now answers without pairing; keeping the old
                # token would only fail the next login and loop back to reauth.
                data.pop(CONF_TOKEN, None)
            else:
                data[CONF_TOKEN] = self._token.hex()

            return self.async_update_reload_and_abort(entry, data=data)

        data = {CONF_ADDRESS: self._address}
        if self._token is not None:
            data[CONF_TOKEN] = self._token.hex()

        return self.async_create_entry(title=self._title or "", data=data)

    @staticmethod
    @callback
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
            # The selector hands back a float; the interval is whole seconds.
            return self.async_create_entry(
                data={CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL])}
            )

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
