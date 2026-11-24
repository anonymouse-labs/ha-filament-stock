"""Constants for Filament Stock."""

from typing import Final

DOMAIN: Final = "filament_stock"

CONF_ACCOUNT: Final = "account"
CONF_TOKEN: Final = "token"  # noqa: S105 - config key name
CONF_TOKEN_EXPIRES: Final = "token_expires"  # noqa: S105 - config key name
CONF_SERVICE_ACCOUNT: Final = "service_account"
CONF_FOLDER_ID: Final = "folder_id"
CONF_QUEUE_INTERVAL: Final = "queue_interval"
CONF_SPOOL_INTERVAL: Final = "spool_interval"
CONF_MATCH_NEAREST: Final = "match_nearest"
CONF_MATCH_DISTANCE: Final = "match_distance"

DEFAULT_QUEUE_INTERVAL: Final = 10  # minutes
DEFAULT_SPOOL_INTERVAL: Final = 15  # minutes
TOKEN_WARN_DAYS: Final = 14
DEFAULT_MATCH_DISTANCE: Final = 10.0  # CIEDE2000

STORAGE_VERSION: Final = 1

GROUP_KINDS: Final = ("stock", "needed", "balance")

REPO_URL: Final = "https://github.com/anonymouse-labs/ha-filament-stock"
DRIVE_SETUP_URL: Final = f"{REPO_URL}#google-drive-setup"
