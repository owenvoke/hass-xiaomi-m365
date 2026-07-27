"""Exceptions for the Xiaomi M365 Scooter integration."""

from homeassistant.exceptions import HomeAssistantError


class M365Error(HomeAssistantError):
    """Base exception for the Xiaomi M365 integration."""


class M365DeviceAsleep(M365Error):
    """No connectable advertisement — the scooter is switched off or out of range.

    Kept distinct from the connection and communication errors because it is
    the scooter's normal resting state, not a fault: it should not mark
    entities unavailable or log at error level.
    """


class M365ConnectionError(M365Error):
    """Could not establish a BLE connection to the scooter."""


class M365CommunicationError(M365Error):
    """Connected, but the scooter did not answer with a usable frame."""


class M365AuthError(M365Error):
    """The stored pairing token is not this scooter's any more.

    Distinct from a connection fault because retrying cannot help: the scooter
    keeps a single binding, so pairing it with the Mi Home app or ScooterHacking
    Utility replaces ours and the token has to be established again by hand.
    """
