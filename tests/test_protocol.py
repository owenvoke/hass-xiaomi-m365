"""Tests for the plain M365 framing and register parsers."""

import pytest

from custom_components.xiaomi_m365.protocol import (
    REG_FIRMWARE,
    REG_GENERAL_INFO,
    REG_LOCK,
    Command,
    Device,
    Frame,
    FrameReader,
    build_read,
    build_write,
    parse,
)

from . import bms_info_frame, general_info_frame, lock_state_frame

# The Mi Home app's own read of the 0xB0 block, as captured.
GENERAL_INFO_READ = bytes.fromhex("55 aa 03 20 01 b0 20 0b ff")


def test_build_read_matches_capture() -> None:
    """A built read is byte for byte what the app sends."""
    assert build_read(Device.ESC, REG_GENERAL_INFO, 0x20) == GENERAL_INFO_READ


def test_build_write() -> None:
    """A write carries the write command and its payload."""
    frame = build_write(Device.ESC, REG_LOCK, b"\x01\x00")
    assert FrameReader().feed(frame) == [
        Frame(
            device=Device.ESC,
            command=Command.WRITE,
            register=REG_LOCK,
            payload=b"\x01\x00",
        )
    ]


def test_reader_reassembles_split_frame() -> None:
    """A frame split across notifications comes out whole."""
    reader = FrameReader()
    assert reader.feed(GENERAL_INFO_READ[:4]) == []
    assert reader.feed(GENERAL_INFO_READ[4:]) == [
        Frame(
            device=Device.ESC,
            command=Command.READ,
            register=REG_GENERAL_INFO,
            payload=b"\x20",
        )
    ]


def test_reader_splits_packed_frames() -> None:
    """Two frames in one notification come out as two."""
    frames = FrameReader().feed(GENERAL_INFO_READ * 2)
    assert len(frames) == 2


def test_reader_keeps_header_split_across_chunks() -> None:
    """A trailing 0x55 survives until the 0xAA arrives."""
    reader = FrameReader()
    assert reader.feed(b"\x01\x02" + GENERAL_INFO_READ[:1]) == []
    assert len(reader.feed(GENERAL_INFO_READ[1:])) == 1


def test_reader_resynchronises_after_bad_checksum() -> None:
    """A corrupt frame is dropped and the next good one still parses."""
    corrupt = GENERAL_INFO_READ[:-1] + b"\x00"
    assert len(FrameReader().feed(corrupt + GENERAL_INFO_READ)) == 1


def test_reader_reset_drops_partial_frame() -> None:
    """Reset throws away half a frame."""
    reader = FrameReader()
    reader.feed(GENERAL_INFO_READ[:5])
    reader.reset()
    assert reader.feed(GENERAL_INFO_READ[5:]) == []


def test_parse_general_info() -> None:
    """The 0xB0 block yields battery, speeds, odometer and temperature."""
    assert parse(general_info_frame()) == {
        "battery": 47,
        "speed": 12.3,
        "average_speed": 10.0,
        "odometer": 123.45,
        "frame_temperature": 21.5,
    }


@pytest.mark.parametrize(
    ("locked", "expected"), [(True, True), (False, False)], ids=["locked", "unlocked"]
)
def test_parse_lock_state(locked: bool, expected: bool) -> None:
    """Only the first byte decides the lock state; the flag byte is ignored."""
    assert parse(lock_state_frame(locked)) == {"is_locked": expected}


def test_parse_bms_info() -> None:
    """The BMS block reports the hotter cell probe, less its offset."""
    assert parse(bms_info_frame()) == {
        "battery": 47,
        "battery_capacity": 3_400,
        "battery_current": -1.5,
        "battery_voltage": 38.5,
        "battery_temperature": 23,
    }


def test_parse_firmware() -> None:
    """Firmware 0x0134 reads as 1.3.4."""
    frame = Frame(Device.ESC_REPLY, Command.READ, REG_FIRMWARE, b"\x34\x01")
    assert parse(frame) == {"firmware_version": "1.3.4"}


def test_parse_keys_on_device_as_well_as_register() -> None:
    """0x31 is the battery block on the BMS, and means nothing on the ESC."""
    frame = bms_info_frame()
    esc_frame = Frame(Device.ESC_REPLY, frame.command, frame.register, frame.payload)
    assert parse(esc_frame) == {}


def test_parse_ignores_short_payload() -> None:
    """A truncated reply yields nothing rather than garbage."""
    frame = Frame(Device.ESC_REPLY, Command.READ, REG_GENERAL_INFO, b"\x00" * 8)
    assert parse(frame) == {}
