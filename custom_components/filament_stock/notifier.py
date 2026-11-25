"""Notifications: new spool, negative or low balance, unsliced file, token expiry.

Each event is sent once. A balance alert re-arms only after the balance
recovers. Events within a short window are combined into one message, and
at most 20 messages per minute are sent (the WhatsApp Notifier limit).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import logging
import time
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.translation import async_get_translations

from .const import (
    CONF_NOTIFY_CHAT_ID,
    CONF_NOTIFY_ENTITY,
    CONF_NOTIFY_LOW,
    CONF_NOTIFY_METHOD,
    CONF_NOTIFY_NEGATIVE,
    CONF_NOTIFY_NEW_SPOOL,
    CONF_NOTIFY_NOT_SLICED,
    CONF_NOTIFY_TOKEN,
    CONF_NOTIFY_UNMATCHED,
    CONF_TOKEN_EXPIRES,
    DOMAIN,
    METHOD_NOTIFY_ENTITY,
    METHOD_PERSISTENT,
    METHOD_WHATSAPP,
    STORAGE_VERSION,
    TOKEN_WARN_DAYS,
    WHATSAPP_DOMAIN,
)
from .stock import StockManager

_LOGGER = logging.getLogger(__name__)

COALESCE_SECONDS = 30
RATE_LIMIT = 20  # messages
RATE_WINDOW = 60  # seconds
MAX_LINES = 25  # per message


class Notifier:
    """Decide what to tell the user and send it."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, manager: StockManager
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._manager = manager
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.notify"
        )
        self.negative: set[str] = set()
        self.low: set[str] = set()
        self.not_sliced: set[str] = set()
        self.unmatched: set[str] = set()
        self.token_warned: float = 0
        self.pending: list[str] = []
        self.sent: deque[float] = deque()
        self._texts: dict[str, str] = {}
        self._handled_sync: object = None
        self._flush_handle: CALLBACK_TYPE | None = None

    # Setup

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.negative = set(stored.get("negative", []))
        self.low = set(stored.get("low", []))
        self.not_sliced = set(stored.get("not_sliced", []))
        self.unmatched = set(stored.get("unmatched", []))
        self.token_warned = float(stored.get("token_warned", 0))
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "exceptions", [DOMAIN]
        )
        prefix = f"component.{DOMAIN}.exceptions."
        self._texts = {
            key.removeprefix(prefix).removesuffix(".message"): text
            for key, text in translations.items()
            if key.startswith(prefix) and key.endswith(".message")
        }

    def _to_store(self) -> dict[str, Any]:
        return {
            "negative": sorted(self.negative),
            "low": sorted(self.low),
            "not_sliced": sorted(self.not_sliced),
            "unmatched": sorted(self.unmatched),
            "token_warned": self.token_warned,
        }

    async def async_flush_store(self) -> None:
        await self._store.async_save(self._to_store())

    @callback
    def async_start(self) -> list[Callable[[], None]]:
        return [self._manager.add_listener(self.async_evaluate), self._cancel]

    @callback
    def _cancel(self) -> None:
        if self._flush_handle:
            self._flush_handle()
            self._flush_handle = None

    def _text(self, key: str, **values: Any) -> str:
        template = self._texts.get(f"notify_{key}", key)
        try:
            return template.format(**values)
        except KeyError, IndexError, ValueError:
            return template

    # Decide

    @callback
    def async_evaluate(self) -> None:
        """Look for new events. Only on complete data, or a restart could re-alert."""
        manager = self._manager
        if not manager.ready:
            return
        options = self._entry.options
        lines: list[str] = []

        sync = manager.last_sync
        if sync is not None and sync is not self._handled_sync:
            self._handled_sync = sync
            if options.get(CONF_NOTIFY_NEW_SPOOL, True) and not sync.first:
                lines.extend(self._new_spools(sync.new))

        threshold = float(options.get(CONF_NOTIFY_LOW, 0) or 0)
        for group_id, item in manager.state.items():
            balance = round(item.balance, 1)
            values = {
                "group": item.label,
                "balance": f"{balance:g}",
                "stock": f"{round(item.stock):g}",
                "needed": f"{round(item.needed):g}",
                "threshold": f"{threshold:g}",
            }
            if balance < 0:
                if group_id not in self.negative:
                    self.negative.add(group_id)
                    if options.get(CONF_NOTIFY_NEGATIVE, True):
                        lines.append(self._text("negative", **values))
            else:
                self.negative.discard(group_id)
            if threshold > 0 and balance < threshold:
                if group_id not in self.low:
                    self.low.add(group_id)
                    if balance >= 0:  # negative already has its own alert
                        lines.append(self._text("low", **values))
            else:
                self.low.discard(group_id)
        for gone in (self.negative | self.low) - set(manager.state):
            self.negative.discard(gone)
            self.low.discard(gone)

        unmatched = {
            key: color
            for key, color in manager.file_colors.items()
            if not color.match.matched
        }
        for key in sorted(set(unmatched) - self.unmatched):
            if options.get(CONF_NOTIFY_UNMATCHED, True):
                lines.append(self._unmatched_text(unmatched[key]))
        self.unmatched = set(unmatched)

        files = set(manager.queue.data.not_sliced) if manager.queue.data else set()
        for name in sorted(files - self.not_sliced):
            if options.get(CONF_NOTIFY_NOT_SLICED, True):
                lines.append(self._text("not_sliced", file=name))
        self.not_sliced = files

        expires = float(self._entry.data.get(CONF_TOKEN_EXPIRES, 0))
        days = (expires - time.time()) / 86400
        if days <= TOKEN_WARN_DAYS and self.token_warned != expires:
            self.token_warned = expires
            if options.get(CONF_NOTIFY_TOKEN, True):
                lines.append(self._text("token", days=max(0, int(days))))

        self._store.async_delay_save(self._to_store, 2)
        if lines:
            self.pending.extend(lines)
            self._schedule(COALESCE_SECONDS)

    def _unmatched_text(self, color: Any) -> str:
        values = {
            "color": color.label,
            "material": color.match.material,
            "hex": color.match.color,
            "files": ", ".join(color.files[:3]) or "-",
        }
        if color.suggestion:
            group_id, distance = color.suggestion
            item = self._manager.state.get(group_id)
            values["nearest"] = item.label if item else group_id
            values["distance"] = f"{distance:g}"
            return self._text("unmatched_nearest", **values)
        return self._text("unmatched_new", **values)

    def _new_spools(self, spool_ids: list[int]) -> list[str]:
        lines = []
        by_id = {
            s["id"]: (item, s)
            for item in self._manager.state.values()
            for s in item.spools
        }
        for spool_id in spool_ids:
            if spool_id not in by_id:
                continue
            item, spool = by_id[spool_id]
            lines.append(
                self._text(
                    "new_spool",
                    group=item.label,
                    name=spool.get("name") or "-",
                    grams=f"{round(spool['remaining']):g}",
                )
            )
        return lines

    # Send

    @callback
    def _schedule(self, delay: float) -> None:
        if self._flush_handle is None:
            self._flush_handle = async_call_later(self.hass, delay, self._on_timer)

    @callback
    def _on_timer(self, _now: Any) -> None:
        self._flush_handle = None
        self._entry.async_create_task(self.hass, self.async_send_pending())

    async def async_send_pending(self) -> None:
        if not self.pending:
            return
        now = time.monotonic()
        while self.sent and now - self.sent[0] > RATE_WINDOW:
            self.sent.popleft()
        if len(self.sent) >= RATE_LIMIT:
            self._schedule(RATE_WINDOW - (now - self.sent[0]) + 1)
            return
        lines, self.pending = self.pending[:MAX_LINES], self.pending[MAX_LINES:]
        self.sent.append(now)
        await self.async_send("\n".join(lines))
        if self.pending:
            self._schedule(1)

    async def async_send(self, message: str) -> None:
        """Send by the chosen method; fall back to a persistent notification."""
        options = self._entry.options
        method = options.get(CONF_NOTIFY_METHOD, METHOD_PERSISTENT)
        title = self._text("title")
        try:
            if method == METHOD_WHATSAPP:
                data: dict[str, Any] = {"message": f"*{title}*\n{message}"}
                if chat := options.get(CONF_NOTIFY_CHAT_ID):
                    data["chat_id"] = chat
                await self.hass.services.async_call(
                    WHATSAPP_DOMAIN, "send_message", data, blocking=True
                )
                return
            if method == METHOD_NOTIFY_ENTITY and options.get(CONF_NOTIFY_ENTITY):
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {
                        "entity_id": options[CONF_NOTIFY_ENTITY],
                        "title": title,
                        "message": message,
                    },
                    blocking=True,
                )
                return
        except Exception as err:  # noqa: BLE001 - a failed alert must not break stock
            _LOGGER.warning("Notification via %s failed: %s", method, err)
        persistent_notification.async_create(
            self.hass,
            message,
            title=title,
            notification_id=f"{DOMAIN}_{int(time.time() * 1000)}",
        )
