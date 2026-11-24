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
CONF_NOTIFY_METHOD: Final = "notify_method"
CONF_NOTIFY_ENTITY: Final = "notify_entity"
CONF_NOTIFY_CHAT_ID: Final = "notify_chat_id"
CONF_NOTIFY_NEW_SPOOL: Final = "notify_new_spool"
CONF_NOTIFY_NEGATIVE: Final = "notify_negative"
CONF_NOTIFY_LOW: Final = "notify_low"
CONF_NOTIFY_NOT_SLICED: Final = "notify_not_sliced"
CONF_NOTIFY_TOKEN: Final = "notify_token"  # noqa: S105 - config key name

METHOD_PERSISTENT: Final = "persistent"
METHOD_NOTIFY_ENTITY: Final = "notify_entity"
METHOD_WHATSAPP: Final = "whatsapp"
WHATSAPP_DOMAIN: Final = "whatsapp_notifier"
WHATSAPP_URL: Final = "https://github.com/anonymouse-labs/ha-whatsapp-notifier"

DEFAULT_QUEUE_INTERVAL: Final = 10  # minutes
DEFAULT_SPOOL_INTERVAL: Final = 15  # minutes
TOKEN_WARN_DAYS: Final = 14
DEFAULT_MATCH_DISTANCE: Final = 10.0  # CIEDE2000

STORAGE_VERSION: Final = 1

GROUP_KINDS: Final = ("stock", "needed", "balance")

REPO_URL: Final = "https://github.com/anonymouse-labs/ha-filament-stock"
DRIVE_SETUP_URL: Final = f"{REPO_URL}#google-drive-setup"
