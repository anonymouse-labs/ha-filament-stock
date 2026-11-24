"""Constants for Filament Stock."""

from typing import Final

DOMAIN: Final = "filament_stock"

CONF_SERVICE_ACCOUNT: Final = "service_account"
CONF_FOLDER_ID: Final = "folder_id"
CONF_QUEUE_INTERVAL: Final = "queue_interval"

DEFAULT_QUEUE_INTERVAL: Final = 10  # minutes

STORAGE_VERSION: Final = 1

DRIVE_SETUP_URL: Final = (
    "https://github.com/anonymouse-labs/ha-filament-stock#google-drive-setup"
)
