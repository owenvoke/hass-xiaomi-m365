"""Tests for the Xiaomi BLE authentication cryptography."""

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from custom_components.xiaomi_m365.crypto import (
    ENCRYPTED_HEADER,
    TOKEN_LENGTH,
    EncryptedFrameReader,
    MiSession,
    expand,
    generate_keypair,
    login_material,
    public_key_bytes,
    registration_secret,
)
from custom_components.xiaomi_m365.protocol import REG_GENERAL_INFO, Device, build_read

from . import TOKEN

APP_RANDOM = bytes(range(16))
DEVICE_RANDOM = bytes(range(16, 32))
FRAME = build_read(Device.ESC, REG_GENERAL_INFO, 0x20)


def _sessions() -> tuple[MiSession, MiSession]:
    """Return our session and the scooter's mirror of it."""
    _, _, ours = login_material(TOKEN, APP_RANDOM, DEVICE_RANDOM)
    scooter = MiSession(
        app_key=ours.device_key,
        app_iv=ours.device_iv,
        device_key=ours.app_key,
        device_iv=ours.app_iv,
    )
    return ours, scooter


def test_encrypt_decrypt_round_trip() -> None:
    """What we encrypt, a scooter holding the same token decrypts."""
    ours, scooter = _sessions()
    encrypted = ours.encrypt(FRAME)

    assert encrypted.startswith(ENCRYPTED_HEADER)
    assert scooter.decrypt(encrypted) == FRAME


def test_encryption_is_never_repeated() -> None:
    """Two identical frames encrypt differently, and the counter advances."""
    ours, scooter = _sessions()
    first, second = ours.encrypt(FRAME), ours.encrypt(FRAME)

    assert first != second
    assert first[3:5] == b"\x00\x00"
    assert second[3:5] == b"\x01\x00"
    assert scooter.decrypt(second) == FRAME


def test_login_proofs_differ_by_direction() -> None:
    """Our proof cannot be replayed as the scooter's."""
    proof, expected, _ = login_material(TOKEN, APP_RANDOM, DEVICE_RANDOM)
    assert proof != expected


def test_reader_reassembles_and_decrypts() -> None:
    """Split, noisy encrypted traffic comes out as plain frames."""
    ours, scooter = _sessions()
    reader = EncryptedFrameReader(ours)
    encrypted = scooter.encrypt(FRAME)

    assert reader.feed(b"\x00\x55" + encrypted[:6]) == []
    assert reader.feed(encrypted[6:]) == [FRAME]
    # Once the size is learnt, packed frames split cleanly.
    assert reader.feed(scooter.encrypt(FRAME) + scooter.encrypt(FRAME)) == [
        FRAME,
        FRAME,
    ]


def test_reader_rejects_frames_under_another_key() -> None:
    """A frame that does not decrypt is discarded, not returned."""
    ours, _ = _sessions()
    _, _, stranger = login_material(bytes(12), APP_RANDOM, DEVICE_RANDOM)

    assert EncryptedFrameReader(ours).feed(stranger.encrypt(FRAME)) == []


def test_registration_agrees_with_the_scooter() -> None:
    """Both ends of the ECDH exchange reach the same token and device id."""
    private_key, public_key = generate_keypair()
    device_private = ec.generate_private_key(ec.SECP256R1())
    device_info = b"\x00\x01\x02\x03" + b"blt.3.1abcdefgh"

    encrypted_id, token = registration_secret(
        private_key, public_key_bytes(device_private.public_key()), device_info
    )

    # The scooter's side, from our public key alone.
    ours = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), b"\x04" + public_key
    )
    expanded = expand(
        device_private.exchange(ec.ECDH(), ours), b"mible-setup-info", salt=None
    )

    assert len(token) == TOKEN_LENGTH
    assert token == expanded[:TOKEN_LENGTH]
    assert (
        AESCCM(expanded[28:44], tag_length=4).decrypt(
            bytes(range(16, 28)), encrypted_id, b"devID"
        )
        == device_info[4:]
    )
