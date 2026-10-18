"""Framing and parsing for the Xiaomi M365 (Ninebot) BLE protocol.

Deliberately free of any Home Assistant or bleak import, so the framing can be
read, reasoned about and driven by hand without a scooter or a running Home
Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import logging
import struct
from typing import Any, Final

_LOGGER = logging.getLogger(__name__)

HEADER: Final = b"\x55\xaa"

# Smallest legal value of the length byte: it counts the payload plus the
# command and register bytes, and every frame carries at least one payload byte.
_MIN_LENGTH_BYTE: Final = 3
# Bytes a frame occupies on the wire, on top of the length byte's own count:
# header (2) + length (1) + device (1) + checksum (2).
_FRAME_OVERHEAD: Final = 6


class Device(IntEnum):
    """The "direction" byte — who a frame is addressed to or came from."""

    ESC = 0x20
    """Master to the ESC (motor controller)."""

    ESC_REPLY = 0x23
    """ESC back to the master."""

    BMS = 0x22
    """Master to the battery management system."""

    BMS_REPLY = 0x25
    """BMS back to the master."""


class Command(IntEnum):
    """The frame type byte."""

    READ = 0x01
    WRITE = 0x03


REPLY_FOR: Final[dict[Device, Device]] = {
    Device.ESC: Device.ESC_REPLY,
    Device.BMS: Device.BMS_REPLY,
}

# --- ESC registers ---------------------------------------------------------
#
# 0x30 is deliberately absent. It answers a read, but with 1 where the 0xB0
# block and the BMS independently agree on 47, so whatever it holds it is not
# the battery level. 0x26 and 0x29 answer too and 0x29 does agree with the
# block's odometer, but the block supplies both anyway in a single round trip.
REG_SERIAL: Final = 0x10
REG_FIRMWARE: Final = 0x1A
REG_RANGE_REMAINING: Final = 0x25
REG_TRIP_INFO: Final = 0x3A
REG_LOCK: Final = 0x70
REG_UNLOCK: Final = 0x71
REG_GENERAL_INFO: Final = 0xB0
REG_LOCK_STATE: Final = 0xB2

# --- BMS registers ---------------------------------------------------------
REG_BMS_INFO: Final = 0x31

# Read lengths, in bytes, for each register we poll.
LEN_SERIAL: Final = 14
LEN_FIRMWARE: Final = 2
LEN_RANGE_REMAINING: Final = 2
LEN_TRIP_INFO: Final = 4
LEN_GENERAL_INFO: Final = 32
LEN_LOCK_STATE: Final = 2
LEN_BMS_INFO: Final = 10

# Payload the ESC expects for a lock/unlock write. The register carries the
# intent, not the value, so the same payload serves both — established against
# a real scooter, watching 0xB2 after each attempt:
#
#   0x70 <- 01 00   locks       (0xB2 -> 0x02)
#   0x71 <- 01 00   unlocks     (0xB2 -> 0x00)
#   0x70 <- 00 00   does nothing; the scooter stays locked
#
# That last line is why this is two registers rather than one register with two
# values: writing zero to 0x70 is silently ignored, so a one-register scheme
# can lock a scooter and never release it.
LOCK_WRITE_PAYLOAD: Final = b"\x01\x00"

# Value the lock-state register reports while the motor lock is engaged.
_LOCK_ENGAGED: Final = 0x02


@dataclass(frozen=True, slots=True)
class Frame:
    """A single decoded, checksum-verified protocol frame."""

    device: int
    command: int
    register: int
    payload: bytes


def checksum(body: bytes) -> int:
    """Return the 16-bit checksum for ``body``.

    ``body`` is every byte from the length byte up to, but excluding, the
    two checksum bytes. The algorithm is a plain sum inverted across 16
    bits; the result is transmitted little-endian.
    """
    return (sum(body) ^ 0xFFFF) & 0xFFFF


def _build(device: int, command: int, register: int, payload: bytes) -> bytes:
    body = bytes([len(payload) + 2, device, command, register]) + payload
    return HEADER + body + struct.pack("<H", checksum(body))


def build_read(device: int, register: int, length: int) -> bytes:
    """Build a frame asking ``device`` for ``length`` bytes of ``register``."""
    return _build(device, Command.READ, register, bytes([length]))


def build_write(device: int, register: int, payload: bytes) -> bytes:
    """Build a frame writing ``payload`` to ``register`` on ``device``."""
    return _build(device, Command.WRITE, register, payload)


class FrameReader:
    """Reassembles frames from a stream of BLE notification chunks.

    A single frame is regularly split across notifications, and a single
    notification can carry more than one frame, so callers must not assume
    a one-to-one mapping between a notification and a frame.
    """

    def __init__(self) -> None:
        """Initialize an empty reader."""
        self._buffer = bytearray()

    def reset(self) -> None:
        """Drop any partially received frame."""
        self._buffer.clear()

    def feed(self, data: bytes) -> list[Frame]:
        """Add received bytes and return whatever frames are now complete."""
        self._buffer.extend(data)
        frames: list[Frame] = []

        while True:
            start = self._buffer.find(HEADER)
            if start == -1:
                # Keep a trailing 0x55 — the 0xAA may be in the next chunk.
                tail = 1 if self._buffer[-1:] == HEADER[:1] else 0
                if dropped := len(self._buffer) - tail:
                    # Worth saying out loud: a device that answers in a framing
                    # this parser does not know — a Ninebot-generation scooter
                    # heading each frame 0x5A 0xA5, say — looks identical to a
                    # silent one unless the bytes are logged here.
                    _LOGGER.debug(
                        "Discarding %d byte(s) with no frame header: %s",
                        dropped,
                        bytes(self._buffer[:dropped]).hex(" "),
                    )
                del self._buffer[: len(self._buffer) - tail]
                break

            del self._buffer[:start]

            if len(self._buffer) < 3:
                break

            length = self._buffer[2]
            if length < _MIN_LENGTH_BYTE:
                # Not a real frame start; skip this header and resynchronise.
                del self._buffer[: len(HEADER)]
                continue

            total = length + _FRAME_OVERHEAD
            if len(self._buffer) < total:
                break

            body = bytes(self._buffer[2 : total - 2])
            expected = int.from_bytes(self._buffer[total - 2 : total], "little")
            if expected != checksum(body):
                _LOGGER.debug(
                    "Discarding frame with bad checksum: %s",
                    bytes(self._buffer[:total]).hex(" "),
                )
                del self._buffer[: len(HEADER)]
                continue

            frames.append(
                Frame(
                    device=self._buffer[3],
                    command=self._buffer[4],
                    register=self._buffer[5],
                    payload=bytes(self._buffer[6 : total - 2]),
                )
            )
            del self._buffer[:total]

        return frames


def _decode_string(payload: bytes) -> str | None:
    """Decode an ASCII field, tolerating NUL padding and junk."""
    text = payload.decode("ascii", errors="ignore").replace("\x00", "").strip()
    return text or None


def _parse_general_info(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0xB0 — the block the Mi Home app polls.

    The first eight bytes are error, warning, flags and workmode words that
    we don't surface. Two further fields sit past the temperature (a
    trip-distance-like counter and an uptime counter); trip data comes from
    register 0x3A instead, where both fields are confirmed, so they are
    skipped here rather than giving the same value two sources.
    """
    if len(payload) < 24:
        return {}

    battery, speed, average_speed, odometer = struct.unpack_from("<HhHI", payload, 8)
    (temperature,) = struct.unpack_from("<h", payload, 22)

    return {
        "battery": battery,
        # Both speeds are reported in metres per hour.
        "speed": round(speed / 1000, 1),
        "average_speed": round(average_speed / 1000, 1),
        # Odometer is a metre count that only ever grows.
        "odometer": round(odometer / 1000, 2),
        "frame_temperature": round(temperature / 10, 1),
    }


def _parse_trip_info(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0x3A — seconds and metres for the current trip."""
    if len(payload) < 4:
        return {}

    trip_time, trip_distance = struct.unpack_from("<HH", payload)
    return {"trip_time": trip_time, "trip_distance": trip_distance}


def _parse_range_remaining(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0x25 — the ESC's own range estimate, in km * 100."""
    if len(payload) < 2:
        return {}

    (raw,) = struct.unpack_from("<H", payload)
    return {"range_remaining": round(raw / 100, 2)}


def _parse_lock_state(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0xB2 — motor lock state.

    Only the first byte carries the state. The second is a flag field that
    reads 0x08 on a scooter sitting unlocked, and the same pair turns up at the
    flags position inside the 0xB0 block — so reading the two as one 16-bit
    value makes an ordinary 0x0800 look like an unrecognised lock state.

    Anything other than the known "engaged" value is reported as unlocked
    rather than guessed at.
    """
    if not payload:
        return {}

    raw = payload[0]
    if raw not in (0x00, _LOCK_ENGAGED):
        _LOGGER.debug("Unrecognised lock state value 0x%02x", raw)
    return {"is_locked": raw == _LOCK_ENGAGED}


def _parse_serial(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0x10 — the scooter's serial number, as ASCII."""
    serial = _decode_string(payload)
    return {"serial_number": serial} if serial else {}


def _parse_firmware(payload: bytes) -> dict[str, Any]:
    """Parse ESC register 0x1A — firmware version, e.g. 0x0134 -> "1.3.4"."""
    if len(payload) < 2:
        return {}

    (raw,) = struct.unpack_from("<H", payload)
    return {"firmware_version": f"{raw >> 8 & 0xFF}.{raw >> 4 & 0x0F}.{raw & 0x0F}"}


def _parse_bms_info(payload: bytes) -> dict[str, Any]:
    """Parse BMS register 0x31 — the battery block.

    The battery percentage here and the one in the ESC's 0xB0 block agree in
    every capture; whichever is parsed last wins, which is harmless.
    """
    if len(payload) < 10:
        return {}

    capacity, battery, current, voltage, temp_1, temp_2 = struct.unpack_from(
        "<HHhHBB", payload
    )

    return {
        "battery": battery,
        "battery_capacity": capacity,
        "battery_current": round(current / 100, 2),
        "battery_voltage": round(voltage / 100, 2),
        # Both cell probes are offset by 20 degrees. Report the hotter one —
        # it is the number that matters for a battery.
        "battery_temperature": max(temp_1, temp_2) - 20,
    }


_PARSERS: Final = {
    (Device.ESC_REPLY, REG_GENERAL_INFO): _parse_general_info,
    (Device.ESC_REPLY, REG_TRIP_INFO): _parse_trip_info,
    (Device.ESC_REPLY, REG_RANGE_REMAINING): _parse_range_remaining,
    (Device.ESC_REPLY, REG_LOCK_STATE): _parse_lock_state,
    (Device.ESC_REPLY, REG_SERIAL): _parse_serial,
    (Device.ESC_REPLY, REG_FIRMWARE): _parse_firmware,
    (Device.BMS_REPLY, REG_BMS_INFO): _parse_bms_info,
}


def parse(frame: Frame) -> dict[str, Any]:
    """Turn a frame into coordinator data, keyed on device *and* register.

    The ESC and the BMS number their registers independently, so the device
    byte is part of the lookup — 0x31 means "battery block" on the BMS and
    something else entirely on the ESC.
    """
    parser = _PARSERS.get((frame.device, frame.register))
    if parser is None:
        _LOGGER.debug(
            "No parser for register 0x%02x from device 0x%02x",
            frame.register,
            frame.device,
        )
        return {}

    return parser(frame.payload)
