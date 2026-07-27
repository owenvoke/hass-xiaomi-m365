"""Xiaomi's BLE authentication handshake, over the vendor ``fe95`` service.

Firmware registered to Mi Home relays nothing over the UART until this has
run. Two exchanges live here:

* :meth:`MiAuth.register` runs once, and needs someone standing at the scooter
  to press the power button when it beeps. It hands back a token to keep.
* :meth:`MiAuth.login` runs on every connection, turning that token into the
  session :mod:`crypto` encrypts the UART with.

Both talk over two characteristics on the ``fe95`` service: four-byte commands
announce what is about to happen on one, and bulk data crosses the other in
numbered parcels, each acknowledged. The flow-control words are the same in
both directions — the scooter says "ready" before it will take a parcel, and
"ok" once it has one.

Imports bleak but not Home Assistant, so the handshake can be driven against a
scooter, or a stand-in, without a running Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Final

from bleak import BleakClient

from .crypto import (
    MiSession,
    generate_keypair,
    generate_random_key,
    login_material,
    registration_secret,
)

_LOGGER = logging.getLogger(__name__)

# The two halves of the fe95 service. The names are the firmware's own, and
# have nothing to do with what either protocol is normally for.
UPNP_CHAR_UUID: Final = "00000010-0000-1000-8000-00805f9b34fb"
AVDTP_CHAR_UUID: Final = "00000019-0000-1000-8000-00805f9b34fb"

# Commands announcing what comes next, written to UPNP.
_CMD_GET_INFO: Final = b"\xa2\x00\x00\x00"
_CMD_SET_KEY: Final = b"\x15\x00\x00\x00"
_CMD_LOGIN: Final = b"\x24\x00\x00\x00"
_CMD_AUTH: Final = b"\x13\x00\x00\x00"

# Commands announcing a parcel, written to AVDTP. Each names what is being
# sent, and how the scooter should read it.
_SEND_PUBLIC_KEY: Final = b"\x00\x00\x00\x03\x04\x00"
_SEND_DEVICE_ID: Final = b"\x00\x00\x00\x00\x02\x00"
_SEND_RANDOM_KEY: Final = b"\x00\x00\x00\x0b\x01\x00"
_SEND_PROOF: Final = b"\x00\x00\x00\x0a\x02\x00"

# Flow control, both directions.
_RECV_READY: Final = b"\x00\x00\x01\x01"
_RECV_OK: Final = b"\x00\x00\x01\x00"
_RECV_TIMEOUT: Final = b"\x00\x00\x01\x05\x01\x00"
_RECV_ERROR: Final = b"\x00\x00\x01\x05\x03\x00"

# Outcomes, as a little-endian word on UPNP.
_AUTH_OK: Final = 0x0011
_AUTH_FAILED: Final = 0x0012
_LOGIN_OK: Final = 0x0021
_LOGIN_FAILED: Final = 0x0023

# A parcel chunk carries a two-byte frame number, so 18 bytes of it are data.
_PARCEL_CHUNK: Final = 18

# How long to wait for any single step. The scooter answers each within a few
# hundred milliseconds when it is going to answer at all; the exception is the
# beep during registration, which waits on a person.
_STEP_TIMEOUT: Final = 5.0
_BUTTON_TIMEOUT: Final = 15.0


class MiAuthError(Exception):
    """Raised when the handshake cannot be completed."""


class MiAuth:
    """Runs the handshake on a connection someone else opened."""

    def __init__(self, client: BleakClient, address: str) -> None:
        """Initialize the handshake against an already-connected client."""
        self._client = client
        self._address = address
        self._upnp: asyncio.Queue[bytes] = asyncio.Queue()
        self._avdtp: asyncio.Queue[bytes] = asyncio.Queue()
        self._subscribed = False

    async def start(self) -> None:
        """Subscribe to both halves of the auth service."""
        if self._subscribed:
            return

        await self._client.start_notify(
            UPNP_CHAR_UUID, lambda _c, data: self._upnp.put_nowait(bytes(data))
        )
        await self._client.start_notify(
            AVDTP_CHAR_UUID, lambda _c, data: self._avdtp.put_nowait(bytes(data))
        )
        self._subscribed = True

    async def stop(self) -> None:
        """Unsubscribe, leaving the UART subscription alone."""
        if not self._subscribed:
            return

        self._subscribed = False
        for uuid in (UPNP_CHAR_UUID, AVDTP_CHAR_UUID):
            try:
                await self._client.stop_notify(uuid)
            except Exception as err:  # noqa: BLE001 - tearing down, report only
                _LOGGER.debug(
                    "%s: could not unsubscribe %s: %s", self._address, uuid, err
                )

    async def login(self, token: bytes) -> MiSession:
        """Authenticate with a stored token and return the session keys."""
        await self.start()
        self._drain()

        app_random = generate_random_key()

        await self._write(UPNP_CHAR_UUID, _CMD_LOGIN)
        await self._send_parcel(_SEND_RANDOM_KEY, app_random)

        device_random = await self._receive_parcel()
        device_proof = await self._receive_parcel()

        proof, expected, session = login_material(token, app_random, device_random)
        if device_proof != expected:
            # Either the token is not this scooter's, or something in between
            # is not the scooter at all. Both are fatal; neither is retryable.
            raise MiAuthError(
                "Scooter failed to prove it holds the same token — "
                "the stored token is wrong for this scooter"
            )

        await self._send_parcel(_SEND_PROOF, proof)

        outcome = await self._await_outcome()
        if outcome != _LOGIN_OK:
            raise MiAuthError(f"Login refused by the scooter (0x{outcome:04x})")

        _LOGGER.debug("%s: logged in", self._address)
        return session

    async def register(self) -> bytes:
        """Register with the scooter and return a token to store.

        The scooter beeps partway through and waits for its power button to be
        pressed. Nothing here can do that, so the caller has to have arranged
        for someone to be standing there.
        """
        await self.start()
        self._drain()

        private_key, public_key = generate_keypair()

        await self._write(UPNP_CHAR_UUID, _CMD_GET_INFO)
        device_info = await self._receive_parcel(timeout=_BUTTON_TIMEOUT)

        await self._write(UPNP_CHAR_UUID, _CMD_SET_KEY)
        await self._send_parcel(_SEND_PUBLIC_KEY, public_key)

        device_public_key = await self._receive_parcel()

        encrypted_id, token = registration_secret(
            private_key, device_public_key, device_info
        )
        await self._send_parcel(_SEND_DEVICE_ID, encrypted_id)

        await self._write(UPNP_CHAR_UUID, _CMD_AUTH)

        outcome = await self._await_outcome(timeout=_BUTTON_TIMEOUT)
        if outcome != _AUTH_OK:
            raise MiAuthError(
                f"Registration refused by the scooter (0x{outcome:04x}) — "
                "the power button may not have been pressed in time"
            )

        _LOGGER.debug("%s: registered", self._address)
        return token

    def _drain(self) -> None:
        """Throw away anything left over from an earlier attempt."""
        for queue in (self._upnp, self._avdtp):
            while not queue.empty():
                queue.get_nowait()

    async def _write(self, uuid: str, data: bytes) -> None:
        """Write one command and log it."""
        _LOGGER.debug("%s: auth --> %s %s", self._address, uuid[4:8], data.hex(" "))
        await self._client.write_gatt_char(uuid, data)

    async def _next(self, queue: asyncio.Queue[bytes], timeout: float) -> bytes:
        """Return the next notification from ``queue``, or give up."""
        try:
            data = await asyncio.wait_for(queue.get(), timeout)
        except TimeoutError as err:
            raise MiAuthError(
                f"Scooter went quiet during authentication after {timeout:.0f}s"
            ) from err

        _LOGGER.debug("%s: auth <-- %s", self._address, data.hex(" "))
        return data

    async def _expect_ready(self, timeout: float = _STEP_TIMEOUT) -> None:
        """Wait for the scooter to say it will take a parcel."""
        data = await self._next(self._avdtp, timeout)

        if data.startswith(_RECV_TIMEOUT):
            raise MiAuthError("Scooter timed out waiting for the parcel")
        if data.startswith(_RECV_ERROR):
            raise MiAuthError("Scooter rejected the parcel")
        if not data.startswith(_RECV_READY):
            raise MiAuthError(f"Expected ready, got {data.hex(' ')}")

    async def _send_parcel(self, announce: bytes, payload: bytes) -> None:
        """Announce a parcel, wait to be invited, then send and confirm it."""
        await self._write(AVDTP_CHAR_UUID, announce)
        await self._expect_ready()

        for index in range(0, len(payload), _PARCEL_CHUNK):
            number = index // _PARCEL_CHUNK + 1
            chunk = (
                number.to_bytes(2, "little") + payload[index : index + _PARCEL_CHUNK]
            )
            await self._client.write_gatt_char(AVDTP_CHAR_UUID, chunk)

        data = await self._next(self._avdtp, _STEP_TIMEOUT)
        if not data.startswith(_RECV_OK):
            raise MiAuthError(f"Parcel not acknowledged: {data.hex(' ')}")

    async def _receive_parcel(self, timeout: float = _STEP_TIMEOUT) -> bytes:
        """Collect a numbered parcel and acknowledge it.

        The scooter opens with a frame numbered zero saying how many will
        follow, then sends them in order. Each carries its own number, so a
        gap is detectable rather than silently stitched over.
        """
        header = await self._next(self._avdtp, timeout)
        if int.from_bytes(header[:2], "little") != 0:
            raise MiAuthError(f"Expected a parcel header, got {header.hex(' ')}")

        expected = int.from_bytes(header[4:6], "little")
        await self._client.write_gatt_char(AVDTP_CHAR_UUID, _RECV_READY)

        received = bytearray()
        for number in range(1, expected + 1):
            chunk = await self._next(self._avdtp, _STEP_TIMEOUT)
            if int.from_bytes(chunk[:2], "little") != number:
                raise MiAuthError(
                    f"Parcel frame {number} arrived out of order: {chunk.hex(' ')}"
                )
            received += chunk[2:]

        await self._client.write_gatt_char(AVDTP_CHAR_UUID, _RECV_OK)
        return bytes(received)

    async def _await_outcome(self, timeout: float = _STEP_TIMEOUT) -> int:
        """Return the little-endian outcome word the scooter finishes with."""
        data = await self._next(self._upnp, timeout)
        return int.from_bytes(data[:2], "little")
