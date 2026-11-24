"""Queue coordinator: sliced plates in a Google Drive folder.

Startup never waits on downloads: the last result is loaded from storage
first, changed files are downloaded in the background, in parallel and with
a time-out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_FOLDER_ID,
    CONF_QUEUE_INTERVAL,
    DEFAULT_QUEUE_INTERVAL,
    DOMAIN,
    STORAGE_VERSION,
)
from .drive import (
    MAX_DOWNLOAD_BYTES,
    DriveClient,
    DriveError,
    DriveFile,
    DriveFileTooLarge,
)
from .parser import ParsedFile, ParseStatus, normalize_name, parse_3mf

_LOGGER = logging.getLogger(__name__)

PARALLEL_DOWNLOADS = 3
DOWNLOAD_DEADLINE = 600  # seconds per file, including retries
MAX_KNOWN_FILES = 200
SAVE_DELAY = 2


@dataclass(slots=True)
class QueueFile:
    """One .3mf file in the folder."""

    id: str
    name: str
    stamp: str
    parsed: ParsedFile | None = None  # None: not downloaded (yet)
    error: str | None = None  # last download error key


@dataclass(slots=True)
class KnownFile:
    """A sliced file seen before, kept after it leaves the folder."""

    name: str
    parsed: ParsedFile
    seen: float


@dataclass(slots=True)
class QueueData:
    """State of the folder."""

    folder_name: str = ""
    files: dict[str, QueueFile] = field(default_factory=dict)  # by file id
    other_files: list[str] = field(default_factory=list)

    @property
    def sliced(self) -> list[QueueFile]:
        return [f for f in self.files.values() if f.parsed and f.parsed.sliced]

    @property
    def not_sliced(self) -> list[str]:
        return sorted(
            f.name for f in self.files.values() if f.parsed and not f.parsed.sliced
        )

    @property
    def pending(self) -> list[str]:
        return sorted(f.name for f in self.files.values() if f.parsed is None)

    @property
    def plates(self) -> int:
        return sum(len(f.parsed.plates) for f in self.sliced if f.parsed)

    def usage(self) -> dict[tuple[str, str], float]:
        """Grams per (material, color) over every sliced file."""
        totals: dict[tuple[str, str], float] = {}
        for item in self.sliced:
            if item.parsed is None:
                continue
            for key, grams in item.parsed.usage().items():
                totals[key] = totals.get(key, 0.0) + grams
        return totals


class QueueCoordinator(DataUpdateCoordinator[QueueData]):
    """Poll the Drive folder and parse changed files only."""

    config_entry: ConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: DriveClient
    ) -> None:
        minutes = entry.options.get(CONF_QUEUE_INTERVAL, DEFAULT_QUEUE_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} queue",
            update_interval=timedelta(minutes=minutes),
        )
        self._client = client
        self._folder_id: str = entry.data[CONF_FOLDER_ID]
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.queue"
        )
        self._semaphore = asyncio.Semaphore(PARALLEL_DOWNLOADS)
        self.known: dict[str, KnownFile] = {}

    async def async_load_cache(self) -> None:
        """Load the last result from storage. No network."""
        raw = await self._store.async_load() or {}
        files: dict[str, QueueFile] = {}
        for file_id, item in raw.get("files", {}).items():
            try:
                parsed = item.get("parsed")
                files[file_id] = QueueFile(
                    id=file_id,
                    name=item["name"],
                    stamp=item["stamp"],
                    parsed=ParsedFile.from_dict(parsed) if parsed else None,
                    error=item.get("error"),
                )
            except KeyError, TypeError, ValueError:
                continue
        for key, item in raw.get("known", {}).items():
            try:
                self.known[key] = KnownFile(
                    item["name"], ParsedFile.from_dict(item["parsed"]), item["seen"]
                )
            except KeyError, TypeError, ValueError:
                continue
        self.data = QueueData(
            folder_name=raw.get("folder_name", ""),
            files=files,
            other_files=list(raw.get("other_files", [])),
        )

    def _to_store(self) -> dict[str, Any]:
        data = self.data or QueueData()
        return {
            "folder_name": data.folder_name,
            "other_files": data.other_files,
            "files": {
                f.id: {
                    "name": f.name,
                    "stamp": f.stamp,
                    "parsed": f.parsed.as_dict() if f.parsed else None,
                    "error": f.error,
                }
                for f in data.files.values()
            },
            "known": {
                key: {"name": k.name, "parsed": k.parsed.as_dict(), "seen": k.seen}
                for key, k in self.known.items()
            },
        }

    async def async_flush(self) -> None:
        """Write the cache and the known files now, before an unload."""
        await self._store.async_save(self._to_store())

    async def _fetch(self, item: DriveFile, old: QueueFile | None) -> QueueFile:
        """Download and parse one file. Never raises."""
        result = QueueFile(item.id, item.name, item.stamp)
        async with self._semaphore:
            try:
                async with asyncio.timeout(DOWNLOAD_DEADLINE):
                    raw = await self._client.download(item.id)
            except DriveFileTooLarge:
                result.parsed = ParsedFile(ParseStatus.TOO_LARGE)
                return result
            except (DriveError, TimeoutError) as err:
                _LOGGER.debug(
                    "Download of %s failed: %s", item.name, type(err).__name__
                )
                key = err.key if isinstance(err, DriveError) else "timeout"
                if old is not None and old.parsed is not None:
                    # Keep the previous version of this file until it downloads.
                    return replace(old, name=item.name, error=key)
                result.error = key
                return result
        result.parsed = await self.hass.async_add_executor_job(parse_3mf, raw)
        return result

    async def _async_update_data(self) -> QueueData:
        try:
            folder_name = await self._client.get_folder(self._folder_id)
            listing = await self._client.list_folder(self._folder_id)
        except DriveError as err:
            raise UpdateFailed(f"Google Drive error: {err.key}") from err

        previous = self.data.files if self.data else {}
        data = QueueData(folder_name=folder_name)
        todo: list[tuple[DriveFile, QueueFile | None]] = []
        for item in listing:
            if not item.name.lower().endswith(".3mf"):
                data.other_files.append(item.name)
                continue
            old = previous.get(item.id)
            if old and old.stamp == item.stamp and old.parsed is not None:
                data.files[item.id] = replace(old, name=item.name, error=None)
            elif item.size is not None and item.size > MAX_DOWNLOAD_BYTES:
                data.files[item.id] = QueueFile(
                    item.id, item.name, item.stamp, ParsedFile(ParseStatus.TOO_LARGE)
                )
            else:
                todo.append((item, old))

        for result in await asyncio.gather(*(self._fetch(i, o) for i, o in todo)):
            data.files[result.id] = result

        now = time.time()
        for queued in data.sliced:
            if queued.parsed is None:
                continue
            self.known[normalize_name(queued.name)] = KnownFile(
                queued.name, queued.parsed, now
            )
        if len(self.known) > MAX_KNOWN_FILES:
            keep = sorted(self.known.items(), key=lambda kv: kv[1].seen)[
                -MAX_KNOWN_FILES:
            ]
            self.known = dict(keep)

        data.other_files.sort()
        # Runs after the coordinator stored `data` as self.data.
        self._store.async_delay_save(self._to_store, SAVE_DELAY)
        return data

    def find(self, names: list[str]) -> tuple[str, ParsedFile] | None:
        """Match printer-side names to a sliced file: folder first, then memory."""
        wanted = {normalize_name(n) for n in names if n} - {""}
        if not wanted:
            return None
        if self.data:
            for item in self.data.sliced:
                if normalize_name(item.name) in wanted and item.parsed:
                    return item.name, item.parsed
        for key in wanted:
            if known := self.known.get(key):
                return known.name, known.parsed
        return None
