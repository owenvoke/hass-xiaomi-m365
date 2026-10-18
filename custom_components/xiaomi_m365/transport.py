"""BLE transport for the Xiaomi M365 Scooter.

Owns the connection and the request/response cycle, so the coordinator only
ever deals in registers and parsed values. The connection is kept open
between polls: the scooter takes a second or more to connect, which is a
large slice of a short poll interval to spend reconnecting every time.

Firmware registered to Mi Home relays nothing over the UART until it has been
authenticated, and encrypts everything afterwards. When the entry holds a
token, connecting therefore runs a login and every frame from then on goes
through the session — see ``auth.py`` and ``crypto.py``. Without a token the
same frames go out in the clear, which is what older firmware expects.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.core import HomeAssistant, callback

from .auth import MiAuth, MiAuthError
from .const import NOTIFY_CHAR_UUID, WRITE_CHAR_UUID
from .crypto import EncryptedFrameReader, MiSession
from .exceptions import M365AuthError, M365ConnectionError, M365DeviceAsleep, M365Error
from .protocol import (
    LEN_GENERAL_INFO,
    REG_GENERAL_INFO,
    REPLY_FOR,
    Device,
    Frame,
    FrameReader,
    build_read,
    build_write,
)

_LOGGER = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3
CONNECT_TIMEOUT = 20.0
# The scooter answers a register read in well under a second when it answers
# at all; a longer wait just delays the rest of the poll.
RESPONSE_TIMEOUT = 2.0

# Most a single write may carry. An encrypted frame overruns one ATT payload
# where a plain one never did, and the module ignores anything longer.
MAX_WRITE = 20

# Registration attempts, and how long to wait between them. The scooter beeps
# during the first exchange and then waits to be confirmed by its power button,
# but only honours the press on a later connection — so each attempt drops the
# link and comes back, leaving a window for someone to press it.
REGISTER_ATTEMPTS = 4
REGISTER_RETRY_DELAY = 8.0


class M365Transport:
    """Talks frames to a single scooter over BLE."""

    def __init__(
        self, hass: HomeAssistant, address: str, token: bytes | None = None
    ) -> None:
        """Initialize the transport, authenticating with ``token`` if given."""
        self._hass = hass
        self._address = address
        self._token = token
        self._client: BleakClientWithServiceCache | None = None
        self._reader = FrameReader()
        # Set once a login succeeds, and dropped on disconnect: session keys
        # belong to one connection and mean nothing to the next.
        self._session: MiSession | None = None
        self._encrypted: EncryptedFrameReader | None = None
        self._frames: asyncio.Queue[Frame] = asyncio.Queue()
        self._connect_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()

    @property
    def address(self) -> str:
        """Return the scooter's Bluetooth address."""
        return self._address

    @property
    def is_connected(self) -> bool:
        """Return True while a BLE connection is open."""
        return self._client is not None and self._client.is_connected

    @callback
    def _handle_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        """Forget the client so the next request reconnects."""
        _LOGGER.debug("%s: disconnected", self._address)
        self._client = None
        self._forget_session()

    @callback
    def _handle_notification(
        self, _sender: BleakGATTCharacteristic, data: bytearray
    ) -> None:
        """Feed received bytes to the deframer and queue whole frames.

        A notification is not a frame: the scooter splits long replies such
        as the 0xB0 block across several, and can pack short ones together.
        """
        # Logged raw and before deframing: when a scooter answers with
        # something this protocol does not recognise, these bytes are the only
        # evidence of it, and "no reply" and "an unparseable reply" need very
        # different fixes.
        _LOGGER.debug("%s: notification %s", self._address, data.hex(" "))

        # Authenticated, the bytes are an encrypted frame that unwraps into a
        # plain one; unauthenticated, they are already plain. Either way what
        # reaches the deframer below is ordinary 55 AA traffic.
        if self._encrypted is not None:
            incoming = self._encrypted.feed(bytes(data))
        else:
            incoming = [bytes(data)]

        for plain in incoming:
            for frame in self._reader.feed(plain):
                self._frames.put_nowait(frame)

    async def _async_connect(self) -> BleakClientWithServiceCache:
        """Return a connected client, connecting if necessary."""
        if self._client is not None and self._client.is_connected:
            return self._client

        async with self._connect_lock:
            # Another caller may have connected while we waited for the lock.
            if self._client is not None and self._client.is_connected:
                return self._client

            ble_device = async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is None:
                raise M365DeviceAsleep(
                    f"No connectable advertisement from {self._address} — "
                    "the scooter is switched off or out of range"
                )

            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self._address,
                    disconnected_callback=self._handle_disconnect,
                    max_attempts=CONNECT_ATTEMPTS,
                    use_services_cache=True,
                    ble_device_callback=lambda: async_ble_device_from_address(
                        self._hass, self._address, connectable=True
                    ),
                    timeout=CONNECT_TIMEOUT,
                )
                self._reader.reset()
                # Subscribed before any login: the firmware only honours a UART
                # subscription that predates the session, and silently relays
                # nothing to one taken out afterwards.
                await client.start_notify(NOTIFY_CHAR_UUID, self._handle_notification)
            except (BleakError, EOFError, OSError, TimeoutError) as err:
                raise M365ConnectionError(
                    f"Could not connect to {self._address}: {err}"
                ) from err

            _LOGGER.debug("%s: connected", self._address)

            if self._token is not None:
                await self._async_login(client)

            self._client = client
            return client

    async def _async_login(self, client: BleakClientWithServiceCache) -> None:
        """Authenticate, so the scooter will relay the UART at all."""
        assert self._token is not None

        try:
            self._session = await MiAuth(client, self._address).login(self._token)
        except MiAuthError as err:
            raise M365AuthError(f"{self._address}: {err}") from err
        except (BleakError, EOFError, OSError, TimeoutError) as err:
            raise M365ConnectionError(
                f"Failed to authenticate with {self._address}: {err}"
            ) from err

        self._encrypted = EncryptedFrameReader(self._session)

    @callback
    def _forget_session(self) -> None:
        """Drop the session and any half-received frame."""
        self._session = None
        self._encrypted = None
        self._reader.reset()

    async def _async_send(
        self, client: BleakClientWithServiceCache, frame: bytes
    ) -> None:
        """Encrypt if authenticated, then write the frame out in chunks."""
        if self._session is not None:
            frame = self._session.encrypt(frame)

        _LOGGER.debug("%s: sending %s", self._address, frame.hex(" "))

        for start in range(0, len(frame), MAX_WRITE):
            await client.write_gatt_char(
                WRITE_CHAR_UUID, frame[start : start + MAX_WRITE]
            )

    async def async_register(self) -> bytes:
        """Pair with the scooter and return a token to store.

        The scooter beeps partway through and will go no further until its power
        button is pressed, and only takes the press into account on a later
        connection — so this drops the link and comes back between attempts,
        which is the window someone has to press it in.
        """
        last_error: Exception | None = None

        for attempt in range(1, REGISTER_ATTEMPTS + 1):
            await self.async_disconnect()

            try:
                client = await self._async_connect()
                token = await MiAuth(client, self._address).register()
            except (MiAuthError, M365ConnectionError, BleakError, TimeoutError) as err:
                last_error = err
                _LOGGER.debug(
                    "%s: registration attempt %d/%d failed: %s",
                    self._address,
                    attempt,
                    REGISTER_ATTEMPTS,
                    err,
                )
                if attempt < REGISTER_ATTEMPTS:
                    await asyncio.sleep(REGISTER_RETRY_DELAY)
                continue

            _LOGGER.debug("%s: registered on attempt %d", self._address, attempt)
            self._token = token
            # The session this connection holds was never established, so start
            # afresh: the caller logs in with the new token on the next request.
            await self.async_disconnect()
            return token

        raise M365AuthError(
            f"Could not pair with {self._address} after {REGISTER_ATTEMPTS} "
            f"attempts — the power button may not have been pressed in time "
            f"({last_error})"
        )

    async def async_probe(self) -> bool:
        """Return True when the scooter answers a read as currently configured.

        Used to tell firmware that needs pairing from firmware that does not,
        without asking someone who has no way of knowing.
        """
        try:
            frame = await self.async_request(
                Device.ESC, REG_GENERAL_INFO, LEN_GENERAL_INFO
            )
        except M365AuthError:
            # A rejected token is an answer in itself, and not one to swallow.
            raise
        except M365Error as err:
            _LOGGER.debug("%s: probe failed: %s", self._address, err)
            return False

        return frame is not None

    def _drain(self) -> None:
        """Throw away frames left over from an earlier, timed-out request."""
        while True:
            try:
                stale = self._frames.get_nowait()
            except asyncio.QueueEmpty:
                return
            _LOGGER.debug(
                "%s: discarding stale frame for register 0x%02x",
                self._address,
                stale.register,
            )

    async def async_request(
        self, device: int, register: int, length: int
    ) -> Frame | None:
        """Read ``length`` bytes of ``register`` and return the reply frame.

        Returns None when the scooter does not answer in time. Some
        registers are simply absent on some firmware revisions, so an
        unanswered read is not treated as a hard error — the caller decides
        what a missing value means.
        """
        expected_device = REPLY_FOR[device]

        async with self._request_lock:
            client = await self._async_connect()
            self._drain()

            try:
                await self._async_send(client, build_read(device, register, length))
            except (BleakError, EOFError, OSError, TimeoutError) as err:
                raise M365ConnectionError(
                    f"Failed to send read of register 0x{register:02x}: {err}"
                ) from err

            return await self._async_await_reply(expected_device, register)

    async def _async_await_reply(
        self, expected_device: int, register: int
    ) -> Frame | None:
        """Wait for the reply to the register we just asked about.

        Frames for other registers can still be in flight from a previous
        request, so match on both the device and the register rather than
        taking whatever arrives first.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RESPONSE_TIMEOUT

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                _LOGGER.debug(
                    "%s: no reply for register 0x%02x", self._address, register
                )
                return None

            try:
                frame = await asyncio.wait_for(self._frames.get(), remaining)
            except TimeoutError:
                _LOGGER.debug(
                    "%s: timed out reading register 0x%02x", self._address, register
                )
                return None

            if frame.device == expected_device and frame.register == register:
                return frame

            _LOGGER.debug(
                "%s: ignoring frame for register 0x%02x from device 0x%02x "
                "while waiting for 0x%02x",
                self._address,
                frame.register,
                frame.device,
                register,
            )

    async def async_write(self, device: int, register: int, payload: bytes) -> None:
        """Write ``payload`` to ``register``.

        Writes are not acknowledged on the notify characteristic, so callers
        that need to know whether it took effect have to read the state back.
        """
        async with self._request_lock:
            client = await self._async_connect()

            try:
                await self._async_send(client, build_write(device, register, payload))
            except (BleakError, EOFError, OSError, TimeoutError) as err:
                raise M365ConnectionError(
                    f"Failed to write register 0x{register:02x}: {err}"
                ) from err

    async def async_disconnect(self) -> None:
        """Close the connection, if there is one."""
        client, self._client = self._client, None
        self._forget_session()

        if client is None or not client.is_connected:
            return

        try:
            await client.disconnect()
        except (BleakError, EOFError, OSError, TimeoutError) as err:
            _LOGGER.debug("%s: error while disconnecting: %s", self._address, err)
