"""Cryptography for Xiaomi's BLE authentication, as the M365 uses it.

Firmware registered to Mi Home will not relay anything over the UART until a
handshake completes on the vendor ``fe95`` service, and from then on the UART
is encrypted rather than plain. This module is the maths; the handshake that
drives it is in ``auth.py``.

The scheme, end to end:

* **Registration**, done once, needs a physical button press on the scooter. An
  ECDH exchange on P-256 establishes a shared secret, HKDF-SHA256 expands it,
  and the first twelve bytes are a *token* to keep forever.
* **Login**, done on every connection, expands that token — salted with a
  random value from each side — into a key and an IV per direction. Each side
  proves it derived the same thing by HMAC.
* **Traffic** afterwards is AES-CCM with a four-byte tag, in a frame headed
  ``55 AB`` rather than the plain ``55 AA``.

Like ``protocol.py``, this imports neither Home Assistant nor bleak, so the
whole scheme can be exercised without a scooter. ``cryptography`` is not
declared in the manifest because Home Assistant core depends on it already.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import secrets
import struct
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .protocol import HEADER, checksum

_LOGGER = logging.getLogger(__name__)

# Xiaomi uses AES-CCM with a four-byte tag, not the sixteen-byte default.
_TAG_LENGTH: Final = 4

# HKDF info strings. Which one is used is what separates a registration
# exchange from a login exchange — the salt differs too, but these are the
# labels the firmware expects.
_SETUP_INFO: Final = b"mible-setup-info"
_LOGIN_INFO: Final = b"mible-login-info"

# Fixed nonce and additional data for the one-off device-id encryption that
# registration finishes with. Constant on both sides, so they carry no secrecy
# of their own — they only have to agree with the firmware.
_DID_NONCE: Final = bytes(range(16, 28))
_DID_AAD: Final = b"devID"

# An encrypted frame is headed 55 AB where a plain one is headed 55 AA.
ENCRYPTED_HEADER: Final = b"\x55\xab"

# How many bytes an encrypted frame spends beyond its length byte's own count:
# header (2) + length (1) + counter (2) + four random bytes + tag (4) +
# checksum (2) — sixteen. Confirmed against 374 captured frames in both
# directions, every one of which checksummed at exactly this size. The reader
# below still tries a small range and remembers what fits, in case firmware
# exists that also encrypts the plain frame's checksum.
_MIN_FRAME_EXTRA: Final = 16
_MAX_FRAME_EXTRA: Final = 18

TOKEN_LENGTH: Final = 12
_RANDOM_KEY_LENGTH: Final = 16
_PADDING_LENGTH: Final = 4


def generate_random_key() -> bytes:
    """Return a fresh 16-byte random value for a login exchange."""
    return secrets.token_bytes(_RANDOM_KEY_LENGTH)


def generate_keypair() -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    """Return a new P-256 keypair, the public half as bare coordinates."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key, public_key_bytes(private_key.public_key())


def public_key_bytes(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """Return X and Y with no leading format byte, which is what is sent."""
    encoded = public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return encoded[1:]


def public_key_from_bytes(data: bytes) -> ec.EllipticCurvePublicKey:
    """Rebuild a public key from the bare coordinates the scooter sends."""
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b"\x04" + data)


def expand(secret: bytes, info: bytes, salt: bytes | None) -> bytes:
    """Expand ``secret`` into the 64 bytes every stage of this slices up."""
    return HKDF(algorithm=hashes.SHA256(), length=64, salt=salt, info=info).derive(
        secret
    )


def mac(key: bytes, data: bytes) -> bytes:
    """Return the HMAC-SHA256 the two sides prove themselves with."""
    hmac = HMAC(key, hashes.SHA256())
    hmac.update(data)
    return hmac.finalize()


def registration_secret(
    private_key: ec.EllipticCurvePrivateKey,
    device_public_key: bytes,
    device_info: bytes,
) -> tuple[bytes, bytes]:
    """Finish a registration exchange.

    Returns the encrypted device id to hand back as proof, and the token to
    store. The token is the only part worth keeping — everything a session
    needs is derivable from it and the two random values, once logging in.
    """
    shared = private_key.exchange(ec.ECDH(), public_key_from_bytes(device_public_key))
    expanded = expand(shared, _SETUP_INFO, salt=None)

    token = expanded[0:TOKEN_LENGTH]
    # The scooter wants its own device id handed back encrypted under a key it
    # can derive too — the proof that both sides reached the same secret. The
    # id sits after a four-byte preamble in what it sent us.
    encrypted_id = AESCCM(expanded[28:44], tag_length=_TAG_LENGTH).encrypt(
        _DID_NONCE, device_info[4:], _DID_AAD
    )

    return encrypted_id, token


@dataclass(frozen=True, slots=True)
class MiSession:
    """The session keys a login yields, and the traffic they protect.

    One key and one IV per direction, so a frame is never encrypted under the
    same material as the frame answering it. The counter forms part of the
    nonce and advances once per frame sent, which is why a frozen dataclass
    still carries mutable state.
    """

    app_key: bytes
    app_iv: bytes
    device_key: bytes
    device_iv: bytes
    _counter: list[int] = field(default_factory=lambda: [0])

    @staticmethod
    def _nonce(iv: bytes, counter: int) -> bytes:
        """Return the 12-byte nonce for ``counter`` in one direction."""
        return iv + bytes(4) + struct.pack("<I", counter)

    def encrypt(self, frame: bytes) -> bytes:
        """Wrap a plain ``55 AA`` frame as an encrypted ``55 AB`` one."""
        counter = self._counter[0]
        self._counter[0] += 1

        length = frame[2:3]
        # The plain frame's own checksum is dropped rather than encrypted: the
        # encrypted frame carries its own, so an inner one is redundant, and
        # including it makes the frame two bytes longer than the firmware will
        # accept — it drops such frames without a word. Four random bytes ride
        # along behind the body so two identical reads do not encrypt alike.
        plaintext = frame[3:-2] + secrets.token_bytes(_PADDING_LENGTH)

        ciphertext = AESCCM(self.app_key, tag_length=_TAG_LENGTH).encrypt(
            self._nonce(self.app_iv, counter), plaintext, None
        )

        body = length + struct.pack("<H", counter) + ciphertext
        return ENCRYPTED_HEADER + body + struct.pack("<H", checksum(body))

    def decrypt(self, frame: bytes) -> bytes:
        """Turn an encrypted ``55 AB`` frame back into a plain ``55 AA`` one.

        Handed back in plain form with the checksum recomputed, so the ordinary
        ``FrameReader`` and parsers deal with it knowing none of this.
        """
        if not frame.startswith(ENCRYPTED_HEADER):
            raise ValueError(f"not an encrypted frame: {frame.hex(' ')}")

        length = frame[2]
        counter = int.from_bytes(frame[3:5], "little")

        plaintext = AESCCM(self.device_key, tag_length=_TAG_LENGTH).decrypt(
            self._nonce(self.device_iv, counter), frame[5:-2], None
        )

        # The length byte travels in the clear and is authoritative: it counts
        # the payload plus the command and register bytes. Slicing by it rather
        # than by what is left over means the padding, and any checksum the
        # scooter echoed inside the ciphertext, are ignored rather than
        # mistaken for data.
        body = bytes([length]) + plaintext[:3] + plaintext[3 : length + 1]
        return HEADER + body + struct.pack("<H", checksum(body))


def login_material(
    token: bytes, app_random: bytes, device_random: bytes
) -> tuple[bytes, bytes, MiSession]:
    """Derive a session from the stored token and both sides' random values.

    Returns our proof to send, the scooter's proof as it should arrive, and the
    session itself. Checking its proof is what makes this mutual rather than a
    bare key agreement: a scooter not holding the same token cannot produce it.
    """
    salt = app_random + device_random
    expanded = expand(token, _LOGIN_INFO, salt=salt)

    session = MiSession(
        device_key=expanded[0:16],
        app_key=expanded[16:32],
        device_iv=expanded[32:36],
        app_iv=expanded[36:40],
    )

    proof = mac(session.app_key, salt)
    # The scooter signs the same two values the other way round, so a replay of
    # our own proof will not pass for its one.
    expected = mac(session.device_key, device_random + app_random)

    return proof, expected, session


class EncryptedFrameReader:
    """Reassembles ``55 AB`` frames from notifications and decrypts them.

    The plain ``FrameReader`` can work out where a frame ends from its length
    byte alone. Here it cannot: the ciphertext is a few bytes longer than the
    body it covers, by an amount that depends on what the sender chose to put
    inside. So the first frame is found by trying the plausible lengths and
    keeping whichever both checksums and decrypts — two independent checks, the
    second an AEAD tag, so a wrong guess has no realistic chance of passing.
    Once one fits, the length is known and every later frame uses it directly.
    """

    def __init__(self, session: MiSession) -> None:
        """Initialize the reader for one session's keys."""
        self._session = session
        self._buffer = bytearray()
        self._extra: int | None = None

    def reset(self) -> None:
        """Drop any partially received frame."""
        self._buffer.clear()

    def _candidates(self, length: int) -> list[int]:
        """Return the frame sizes worth trying for this length byte."""
        if self._extra is not None:
            return [length + self._extra]
        return [
            length + extra for extra in range(_MIN_FRAME_EXTRA, _MAX_FRAME_EXTRA + 1)
        ]

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes and return the plain frames now recoverable."""
        self._buffer.extend(data)
        frames: list[bytes] = []

        while True:
            start = self._buffer.find(ENCRYPTED_HEADER)
            if start == -1:
                # Keep a trailing 0x55 — the 0xAB may be in the next chunk.
                tail = 1 if self._buffer[-1:] == ENCRYPTED_HEADER[:1] else 0
                del self._buffer[: len(self._buffer) - tail]
                break

            del self._buffer[:start]

            if len(self._buffer) < 3:
                break

            candidates = self._candidates(self._buffer[2])
            match = self._first_valid(candidates)

            if match is None:
                if len(self._buffer) < max(candidates):
                    break
                # Nothing decrypts here, so this was not a frame start.
                _LOGGER.debug(
                    "Discarding false frame header: %s",
                    bytes(self._buffer[: max(candidates)]).hex(" "),
                )
                del self._buffer[: len(ENCRYPTED_HEADER)]
                continue

            total, plain = match
            if self._extra is None:
                self._extra = total - self._buffer[2]
                _LOGGER.debug(
                    "Encrypted frames run %d bytes beyond the length byte", self._extra
                )

            frames.append(plain)
            del self._buffer[:total]

        return frames

    def _first_valid(self, candidates: list[int]) -> tuple[int, bytes] | None:
        """Return the first candidate size that checksums and decrypts."""
        for total in candidates:
            if len(self._buffer) < total:
                continue

            frame = bytes(self._buffer[:total])
            if checksum(frame[2:-2]) != int.from_bytes(frame[-2:], "little"):
                continue

            try:
                return total, self._session.decrypt(frame)
            except (InvalidTag, ValueError, IndexError):
                continue

        return None
