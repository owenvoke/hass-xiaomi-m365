"""Tests for the Xiaomi M365 Scooter integration."""

import struct
import time

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from custom_components.xiaomi_m365.protocol import (
    REG_BMS_INFO,
    REG_GENERAL_INFO,
    REG_LOCK_STATE,
    Command,
    Device,
    Frame,
)
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "MIScooter1234"
TOKEN = bytes(range(12))


def service_info(
    name: str | None = NAME, address: str = ADDRESS
) -> BluetoothServiceInfoBleak:
    """Return a connectable advertisement, as Bluetooth discovery hands over."""
    return BluetoothServiceInfoBleak(
        name=name or address,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, None),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=time.monotonic(),
        tx_power=None,
    )


def general_info_frame(
    battery: int = 47, speed: int = 12_300, odometer: int = 123_450
) -> Frame:
    """Return an ESC 0xB0 reply carrying the given readings."""
    payload = bytearray(32)
    struct.pack_into("<HhHI", payload, 8, battery, speed, 10_000, odometer)
    struct.pack_into("<h", payload, 22, 215)
    return Frame(
        device=Device.ESC_REPLY,
        command=Command.READ,
        register=REG_GENERAL_INFO,
        payload=bytes(payload),
    )


def lock_state_frame(locked: bool) -> Frame:
    """Return an ESC 0xB2 reply for the given lock state."""
    return Frame(
        device=Device.ESC_REPLY,
        command=Command.READ,
        register=REG_LOCK_STATE,
        payload=bytes([0x02 if locked else 0x00, 0x08]),
    )


def bms_info_frame() -> Frame:
    """Return a BMS 0x31 reply."""
    return Frame(
        device=Device.BMS_REPLY,
        command=Command.READ,
        register=REG_BMS_INFO,
        payload=struct.pack("<HHhHBB", 3_400, 47, -150, 3_850, 41, 43),
    )
