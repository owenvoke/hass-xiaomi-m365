"""DataUpdateCoordinator for the Xiaomi M365 Scooter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL, DOMAIN
from .exceptions import M365AuthError, M365DeviceAsleep, M365Error
from .protocol import (
    LEN_BMS_INFO,
    LEN_FIRMWARE,
    LEN_GENERAL_INFO,
    LEN_LOCK_STATE,
    LEN_RANGE_REMAINING,
    LEN_SERIAL,
    LEN_TRIP_INFO,
    LOCK_WRITE_PAYLOAD,
    REG_BMS_INFO,
    REG_FIRMWARE,
    REG_GENERAL_INFO,
    REG_LOCK,
    REG_LOCK_STATE,
    REG_RANGE_REMAINING,
    REG_SERIAL,
    REG_TRIP_INFO,
    REG_UNLOCK,
    Device,
    parse,
)
from .transport import M365Transport

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION: Final = 1
STORAGE_SAVE_DELAY: Final = 15

# Registers polled every cycle, in the order the Mi Home app reads them. The
# 0xB0 block carries battery, speed, average speed, odometer and frame
# temperature in one read, which is both fewer round trips than reading each
# separately and the only source that agrees with the BMS: the narrow 0x30
# register reports 1 where the block and the BMS both say 47.
POLL_REGISTERS: Final = (
    (Device.ESC, REG_GENERAL_INFO, LEN_GENERAL_INFO),
    (Device.ESC, REG_TRIP_INFO, LEN_TRIP_INFO),
    (Device.ESC, REG_RANGE_REMAINING, LEN_RANGE_REMAINING),
    (Device.ESC, REG_LOCK_STATE, LEN_LOCK_STATE),
    (Device.BMS, REG_BMS_INFO, LEN_BMS_INFO),
)

# Read until they answer, then left alone — neither changes while riding.
STATIC_REGISTERS: Final = (
    (Device.ESC, REG_SERIAL, LEN_SERIAL),
    (Device.ESC, REG_FIRMWARE, LEN_FIRMWARE),
)
STATIC_KEYS: Final = ("serial_number", "firmware_version")
# Give up after a few polls so a firmware that ignores these registers does
# not pay two timeouts on every cycle for the rest of the entry's life.
MAX_STATIC_ATTEMPTS: Final = 3

# Momentary readings. Restoring these after a restart would show a speed or a
# battery current from whenever Home Assistant last ran, which is worse than
# showing nothing. Cumulative and configured values are safe to restore: the
# odometer only grows, and the lock stays engaged across a power cycle.
UNPERSISTED_KEYS: Final = frozenset(
    {
        "speed",
        "average_speed",
        "trip_distance",
        "trip_time",
        "frame_temperature",
        "battery_current",
        "battery_temperature",
    }
)


def _storage_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}"


async def async_remove_stored_data(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the restore cache for an entry being removed for good."""
    await Store(hass, STORAGE_VERSION, _storage_key(entry_id)).async_remove()


class M365DataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the scooter and holds its last known state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        transport: M365Transport,
    ) -> None:
        """Initialize the coordinator."""
        interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=entry.title,
            update_interval=timedelta(seconds=interval),
            config_entry=entry,
        )
        self.transport = transport
        self.address = transport.address
        self.data = {}
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, _storage_key(entry.entry_id)
        )
        self._static_attempts = 0
        self._registry_identity: tuple[str | None, str | None] = (None, None)

    async def async_load_stored_data(self) -> None:
        """Seed state from the restore cache.

        The scooter is switched off most of the time, so without this every
        restart would blank the battery level and odometer until the next ride.
        """
        stored = await self._store.async_load()
        if stored:
            self.data = dict(stored)
            _LOGGER.debug("%s: restored %d value(s)", self.address, len(self.data))

    def _save_data(self) -> None:
        """Queue a debounced write of the values worth restoring."""
        self._store.async_delay_save(
            lambda: {k: v for k, v in self.data.items() if k not in UNPERSISTED_KEYS},
            STORAGE_SAVE_DELAY,
        )

    async def async_shutdown(self) -> None:
        """Close the BLE connection when the entry unloads."""
        await super().async_shutdown()
        await self.transport.async_disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        """Poll the scooter and merge whatever it answered into the state.

        A switched-off scooter is the normal case, not a failure, so the
        previous state is returned untouched and the restored battery level
        and odometer stay on show. Anything else — a refused connection, a
        connection that drops mid-poll — is a real fault.
        """
        try:
            values = await self._async_read_registers()
        except M365DeviceAsleep as err:
            _LOGGER.debug("%s: %s", self.address, err)
            return self.data
        except M365AuthError as err:
            # Retrying cannot recover a token the scooter no longer honours, so
            # ask for it to be paired again rather than failing every poll.
            raise ConfigEntryAuthFailed(str(err)) from err
        except M365Error as err:
            raise UpdateFailed(str(err)) from err

        if not values:
            # The connection came up but nothing was answered, which is not
            # something a working scooter does.
            raise UpdateFailed(
                f"Connected to {self.address} but no register was answered"
            )

        data = {**self.data, **values, "last_seen": datetime.now(UTC).isoformat()}
        self.data = data
        self._save_data()
        self._async_update_device_identity()

        return data

    async def _async_read_registers(self) -> dict[str, Any]:
        """Read every polled register and parse the replies."""
        values: dict[str, Any] = {}

        if self._should_read_static():
            self._static_attempts += 1
            for device, register, length in STATIC_REGISTERS:
                frame = await self.transport.async_request(device, register, length)
                if frame is not None:
                    values.update(parse(frame))

        for device, register, length in POLL_REGISTERS:
            frame = await self.transport.async_request(device, register, length)
            if frame is not None:
                values.update(parse(frame))

        return values

    def _should_read_static(self) -> bool:
        """Return True while the serial and firmware are still unknown."""
        if all(self.data.get(key) for key in STATIC_KEYS):
            return False
        return self._static_attempts < MAX_STATIC_ATTEMPTS

    def _async_update_device_identity(self) -> None:
        """Push serial number and firmware version into the device registry.

        Both arrive from the scooter after the device row already exists, so
        they cannot come from ``DeviceInfo`` on a first run.
        """
        identity = (self.data.get("serial_number"), self.data.get("firmware_version"))
        if identity == self._registry_identity or not any(identity):
            return

        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.address)})
        if device is None:
            return

        serial, firmware = identity
        registry.async_update_device(
            device.id,
            serial_number=serial or device.serial_number,
            sw_version=firmware or device.sw_version,
        )
        self._registry_identity = identity

    async def async_set_lock(self, locked: bool) -> None:
        """Engage or release the motor lock.

        Locking and unlocking are separate registers rather than two values
        written to one — writing zero to the lock register is simply ignored,
        so the scooter would never come back. Neither write is acknowledged, so
        the state has to be read back, which the refresh below does.
        """
        register = REG_LOCK if locked else REG_UNLOCK
        await self.transport.async_write(Device.ESC, register, LOCK_WRITE_PAYLOAD)

        # Show the requested state straight away; the refresh confirms it.
        self.async_set_updated_data({**self.data, "is_locked": locked})
        await self.async_request_refresh()
