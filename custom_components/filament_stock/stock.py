"""Stock manager: combines cloud spools, the print queue and the local ledger.

Owns the persistent ledger and the fixed color mappings. Entities read the
computed state per color group and listen for changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    CONF_MATCH_DISTANCE,
    CONF_MATCH_NEAREST,
    DEFAULT_MATCH_DISTANCE,
    DOMAIN,
    STORAGE_VERSION,
)
from .ledger import Deduction, Ledger, SyncResult
from .matcher import Match, Needs, fixed_key, map_needs, nearest_group
from .models import GroupKey
from .parser import UsageKey
from .print_queue import QueueCoordinator
from .spools import SpoolCoordinator

_LOGGER = logging.getLogger(__name__)

UNKNOWN_PREFIX = "unknown_"
SAVE_DELAY = 2


@dataclass(slots=True)
class GroupState:
    """Computed numbers for one color group."""

    id: str
    material: str
    color: str
    note: str = ""
    stock: float = 0.0
    needed: float = 0.0
    spools: list[dict[str, Any]] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unknown(self) -> bool:
        return self.id.startswith(UNKNOWN_PREFIX)

    @property
    def balance(self) -> float:
        return self.stock - self.needed

    @property
    def label(self) -> str:
        return " ".join(p for p in (self.material, self.color, self.note) if p)


@dataclass(slots=True)
class FileColor:
    """A color used by sliced files in the queue, and how it is counted."""

    key: str  # stable id, e.g. 'pla_c12e1f' or 'pla_ffffff_gfa01'
    match: Match
    filament_id: str  # slicer profile id, '' when the file has none
    files: list[str]
    suggestion: tuple[str, float] | None  # nearest group at any distance

    @property
    def label(self) -> str:
        label = f"{self.match.material} {self.match.color}"
        return f"{label} {self.filament_id}" if self.filament_id else label


def file_color_key(material: str, color: str, filament_id: str) -> str:
    """Stable id per file color and filament profile, e.g. 'pla_ffffff_gfa01'."""
    base = GroupKey.of(material, color, "").id
    slug = re.sub(r"[^a-z0-9]+", "_", filament_id.lower()).strip("_")
    return f"{base}_{slug}" if slug else base


def _source(match: Match, filament_id: str, files: list[str]) -> dict[str, Any]:
    source: dict[str, Any] = {
        "color": match.color,
        "method": match.method.value,
        "files": files,
    }
    if filament_id:
        source["filament_id"] = filament_id
    if match.distance is not None:
        source["distance"] = match.distance
    if match.candidates:
        source["candidates"] = list(match.candidates)
    return source


class StockManager:
    """Single source of the numbers shown in Home Assistant."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        spools: SpoolCoordinator,
        queue: QueueCoordinator,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self.spools = spools
        self.queue = queue
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.stock"
        )
        self.ledger = Ledger()
        self.fixed: dict[str, str] = {}
        # material and color of 'unknown' groups that only exist as a shortage
        self.unknown_meta: dict[str, tuple[str, str]] = {}
        self.state: dict[str, GroupState] = {}
        self.needs = Needs()
        self.file_colors: dict[str, FileColor] = {}
        self.last_sync: SyncResult | None = None
        # True once each source holds real data (stored or freshly fetched).
        self.spools_ready = False
        self.queue_ready = False
        self._listeners: list[CALLBACK_TYPE] = []

    # Persistence

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.ledger = Ledger.from_dict(stored.get("ledger"))
        self.fixed = {str(k): str(v) for k, v in (stored.get("fixed") or {}).items()}
        self.unknown_meta = {
            str(k): (str(v[0]), str(v[1]))
            for k, v in (stored.get("unknown_meta") or {}).items()
            if isinstance(v, list) and len(v) == 2
        }
        # A stored list comes from an earlier successful fetch: safe to sync.
        if self.spools.data and self.spools.data.raw:
            self.ledger.sync(self.spools.data.spools)
            self.spools_ready = True
        self.queue_ready = bool(self.queue.data and self.queue.data.files)
        self._recompute()

    def _to_store(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger.as_dict(),
            "fixed": dict(self.fixed),
            "unknown_meta": {k: list(v) for k, v in self.unknown_meta.items()},
        }

    @callback
    def async_save(self) -> None:
        self._store.async_delay_save(self._to_store, SAVE_DELAY)

    async def async_flush(self) -> None:
        """Write now. A reload must not drop bookings still waiting to be saved."""
        await self._store.async_save(self._to_store())

    # Listeners

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """Follow both coordinators. This also keeps their polling active."""
        return [
            self.spools.async_add_listener(self._on_spools),
            self.queue.async_add_listener(self._on_queue),
        ]

    @callback
    def add_listener(self, update: CALLBACK_TYPE) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    @callback
    def _on_spools(self) -> None:
        if self.spools.last_update_success and self.spools.data is not None:
            self.last_sync = self.ledger.sync(self.spools.data.spools)
            # A shortage on a group that no longer exists (note edited) is void.
            groups = self.spools.data.groups
            for group_id in list(self.ledger.shortage):
                if group_id not in groups and not group_id.startswith(UNKNOWN_PREFIX):
                    self.ledger.clear_shortage(group_id)
            self.spools_ready = True
            self.async_save()
        self.async_update()

    @callback
    def _on_queue(self) -> None:
        if self.queue.last_update_success:
            self.queue_ready = True
        self.async_update()

    @property
    def ready(self) -> bool:
        """Both sources hold real data: vanished groups may be removed."""
        return self.spools_ready and self.queue_ready

    @callback
    def async_update(self) -> None:
        """Recompute and tell the entities."""
        self._recompute()
        for update in list(self._listeners):
            update()

    # Computation

    def _map(self, usage: dict[UsageKey, float], prefer_loaded: bool = False) -> Needs:
        """Map grams per file color to grams per color group."""
        groups = self.spools.data.groups if self.spools.data else {}
        options = self._entry.options
        return map_needs(
            usage,
            groups,
            self.ledger.stock(groups),
            fixed=self.fixed,
            nearest=options.get(CONF_MATCH_NEAREST, True),
            max_distance=options.get(CONF_MATCH_DISTANCE, DEFAULT_MATCH_DISTANCE),
            prefer_loaded=prefer_loaded,
        )

    def _recompute(self) -> None:
        groups = self.spools.data.groups if self.spools.data else {}
        stock = self.ledger.stock(groups)
        self.needs = self._map(self.queue.data.usage() if self.queue.data else {})

        state: dict[str, GroupState] = {}
        for group_id, group in groups.items():
            state[group_id] = GroupState(
                id=group_id,
                material=group.material,
                color=group.color,
                note=group.note,
                spools=[
                    {
                        "id": s.id,
                        "name": s.name,
                        "vendor": s.vendor,
                        "remaining": round(self.ledger.remaining(s.id), 1),
                        "in_printer": s.in_printer,
                    }
                    for s in group.spools
                ],
            )
        files = self._files_by_usage()
        for usage_key, match in self.needs.matches.items():
            filament_id = usage_key[2]
            if not match.matched:
                self.unknown_meta[match.group_id] = (match.material, match.color)
            if match.group_id not in state:
                state[match.group_id] = GroupState(
                    match.group_id, match.material, match.color
                )
            state[match.group_id].sources.append(
                _source(match, filament_id, sorted(files.get(usage_key, ())))
            )
        for group_id in self.ledger.shortage:
            if group_id not in state:
                material, color = self.unknown_meta.get(group_id, ("?", group_id))
                state[group_id] = GroupState(group_id, material, color)

        for group_id, item in state.items():
            item.stock = stock.get(group_id, 0.0)
            item.needed = self.needs.grams.get(group_id, 0.0)
        self.unknown_meta = {k: v for k, v in self.unknown_meta.items() if k in state}
        self.state = state
        self.file_colors = self._file_colors(groups)

    def _files_by_usage(self) -> dict[UsageKey, set[str]]:
        files: dict[UsageKey, set[str]] = {}
        for item in self.queue.data.sliced if self.queue.data else []:
            if item.parsed is None:
                continue
            for usage_key in item.parsed.usage():
                files.setdefault(usage_key, set()).add(item.name)
        return files

    def _file_colors(self, groups: dict[str, Any]) -> dict[str, FileColor]:
        files = self._files_by_usage()
        result: dict[str, FileColor] = {}
        # One entry per color and filament profile: each can be its own spool.
        for usage_key, match in self.needs.matches.items():
            material, color, filament_id = usage_key
            key = file_color_key(material, color, filament_id)
            result[key] = FileColor(
                key=key,
                match=match,
                filament_id=filament_id,
                files=sorted(files.get(usage_key, set())),
                suggestion=nearest_group(material, color, groups),
            )
        return result

    def group_choices(self) -> dict[str, str]:
        """Label to group id, for picking a group in the UI. Spool groups only."""
        choices: dict[str, str] = {}
        for group_id, item in sorted(self.state.items(), key=lambda kv: kv[1].label):
            if item.unknown:
                continue
            label = item.label
            if label in choices:
                label = f"{label} ({group_id})"
            choices[label] = group_id
        return choices

    @property
    def total_stock(self) -> float:
        return sum(entry.remaining for entry in self.ledger.spools.values())

    @property
    def total_needed(self) -> float:
        return sum(self.needs.grams.values())

    def group_title(self, group_id: str) -> str:
        """Name as shown in the stock list: 'PLA · DHM00001 Zwart' or 'PLA #FFFFFF'."""
        item = self.state.get(group_id)
        if item is None:
            material, color = self.unknown_meta.get(group_id, ("", group_id))
            return f"{material} {color}".strip()
        return (
            f"{item.material} · {item.note}"
            if item.note
            else f"{item.material} {item.color}"
        )

    # Booking a print

    async def async_book(
        self, job_id: str, usage: dict[UsageKey, float]
    ) -> tuple[Deduction, Needs]:
        """Deduct a print from the ledger and save at once."""
        mapped = self._map(usage, prefer_loaded=True)
        for match in mapped.unmatched:
            self.unknown_meta[match.group_id] = (match.material, match.color)
        groups = self.spools.data.groups if self.spools.data else {}
        result = self.ledger.deduct(job_id, mapped.grams, groups)
        await self.async_flush()
        self.async_update()
        return result, mapped

    # Actions

    @callback
    def set_fixed(
        self, material: str, color: str, group_id: str | None, filament_id: str = ""
    ) -> None:
        key = fixed_key(material, color, filament_id)
        if group_id is None:
            self.fixed.pop(key, None)
        else:
            self.fixed[key] = group_id
        self.async_save()
        self.async_update()

    @callback
    def reset_group(self, group_id: str, spool_id: int | None = None) -> bool:
        """Forget local use for one spool, or for every spool of a group."""
        item = self.state.get(group_id)
        if item is None:
            return False
        ids = [s["id"] for s in item.spools]
        if spool_id is not None:
            if spool_id not in ids:
                return False
            ids = [spool_id]
        else:
            self.ledger.clear_shortage(group_id)
        for sid in ids:
            self.ledger.reset_spool(sid)
        self.async_save()
        self.async_update()
        return True

    @callback
    def set_group_stock(self, group_id: str, grams: float) -> None:
        """Stock entered in Home Assistant, kept until the cloud weight changes."""
        item = self.state.get(group_id)
        if item is None:
            return
        self.ledger.set_group_stock(group_id, grams, [s["id"] for s in item.spools])
        self.async_save()
        self.async_update()
