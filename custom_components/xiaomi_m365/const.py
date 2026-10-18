"""Constants for the Xiaomi M365 Scooter integration."""

from typing import Final

DOMAIN: Final = "xiaomi_m365"

MANUFACTURER: Final = "Xiaomi"
MODEL: Final = "Mi Electric Scooter (M365)"
DEFAULT_NAME: Final = "Xiaomi M365 Scooter"

CONF_ADDRESS: Final = "address"

# Pairing token, hex-encoded, for firmware that gates the UART behind Xiaomi's
# authentication. Absent on entries for scooters that answer without it.
CONF_TOKEN: Final = "token"  # noqa: S105 - a config key, not a secret

# Seconds between polls. Configurable because the useful rate depends on what
# the scooter is being watched for: speed while riding wants a short interval,
# battery and odometer do not.
CONF_UPDATE_INTERVAL: Final = "update_interval"
DEFAULT_UPDATE_INTERVAL: Final = 15
MIN_UPDATE_INTERVAL: Final = 5
MAX_UPDATE_INTERVAL: Final = 600

# The scooter exposes a Nordic UART service. Frames are written to one
# characteristic and the replies arrive as notifications on the other; the
# names below are from the host's point of view, not Nordic's.
SERVICE_UUID: Final = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
WRITE_CHAR_UUID: Final = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NOTIFY_CHAR_UUID: Final = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Advertised names used by the stock firmware.
LOCAL_NAME_PREFIXES: Final = ("MIScooter", "MIBanner")
