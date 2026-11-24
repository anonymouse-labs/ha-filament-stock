"""Spool coordinator: the filament list from the Bambu Lab cloud.

The last list is stored, so stock and print tracking work right after a
restart, also while the cloud is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bambu_api import BambuAuthError, BambuCloudClient, BambuError, parse_spool
from .const import (
    CONF_SPOOL_INTERVAL,
    CONF_TOKEN_EXPIRES,
    DEFAULT_SPOOL_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
    TOKEN_WARN_DAYS,
)
from .models import ColorGroup, Spool, build_groups

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SpoolData:
    """Spools and their color groups."""

    spools: list[Spool] = field(default_factory=list)
    groups: dict[str, ColorGroup] = field(default_factory=dict)
    raw: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: list[dict[str, Any]]) -> SpoolData:
        spools = [s for s in (parse_spool(h) for h in raw) if s is not None]
        return cls(spools=spools, groups=build_groups(spools), raw=raw)


def token_issue_id(entry_id: str) -> str:
    return f"token_expiring_{entry_id}"


class SpoolCoordinator(DataUpdateCoordinator[SpoolData]):
    """Poll the cloud filament list (read-only)."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: BambuCloudClient
    ) -> None:
        minutes = entry.options.get(CONF_SPOOL_INTERVAL, DEFAULT_SPOOL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} spools",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.spools"
        )

    async def async_load_cache(self) -> None:
        """Load the last list from storage. No network."""
        stored = await self._store.async_load() or {}
        raw = [h for h in stored.get("raw", []) if isinstance(h, dict)]
        self.data = SpoolData.from_raw(raw)

    async def _async_update_data(self) -> SpoolData:
        try:
            _, raw = await self.client.fetch_spools()
        except BambuAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="token_rejected"
            ) from err
        except BambuError as err:
            raise UpdateFailed(f"Bambu cloud error: {err.key}") from err
        if not raw and self.data is not None and self.data.raw:
            # One empty answer must not wipe the ledger and every entity.
            raise UpdateFailed("Bambu cloud returned an empty spool list")
        self.check_token_expiry()
        self._store.async_delay_save(lambda: {"raw": raw}, 2)
        return SpoolData.from_raw(raw)

    async def async_flush(self) -> None:
        """Write the last list now, before an unload."""
        if self.data is not None and self.data.raw:
            await self._store.async_save({"raw": self.data.raw})

    def check_token_expiry(self) -> None:
        """Warn in Repairs before the cloud token expires."""
        expires = float(self.config_entry.data.get(CONF_TOKEN_EXPIRES, 0))
        days = (expires - time.time()) / 86400
        issue_id = token_issue_id(self.config_entry.entry_id)
        if days > TOKEN_WARN_DAYS:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="token_expiring",
            translation_placeholders={"days": str(max(0, int(days)))},
        )
