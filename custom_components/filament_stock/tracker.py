"""Follow prints from the ha-bambulab integration and book the filament used.

Rules per printer (ha-bambulab `print_status`):
- prepare, slicing, running, pause: a job is running. The file and the grams
  per color are snapshotted and stored, so a restart or moving the file out of
  the Drive folder does not lose the deduction.
- finish: book 100 %.
- failed: book the highest progress seen.
- idle, init without finish or failed: drop the job, book nothing.
- offline, unknown, unavailable: keep the job, the printer may just be gone.
- any other value: keep the job and log it.
A job is booked once: the ledger refuses a job id it has already booked.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import logging
import re
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION
from .parser import ParsedFile, UsageKey, normalize_name
from .stock import StockManager

_LOGGER = logging.getLogger(__name__)

BAMBU_DOMAIN = "bambu_lab"
KEYS = ("print_status", "print_progress", "gcode_file", "subtask_name")
ACTIVE = frozenset({"prepare", "slicing", "running", "pause"})
STOPPED = frozenset({"idle", "init"})
HOLD = frozenset({"offline", STATE_UNKNOWN, STATE_UNAVAILABLE})
NO_NAME = frozenset({"", STATE_UNKNOWN, STATE_UNAVAILABLE, "none"})

_PLATE_RE = re.compile(r"plate_(\d+)\.gcode", re.IGNORECASE)


def plate_from_gcode(name: str | None) -> int | None:
    """Plate number from a printer file name like 'Metadata/plate_2.gcode'."""
    match = _PLATE_RE.search(name or "")
    return int(match.group(1)) if match else None


@dataclass(slots=True)
class Job:
    """A running print, stored across restarts."""

    serial: str
    started: float
    names: list[str] = field(default_factory=list)
    subtask: str = ""  # identifies the print; gcode_file is often just plate_1
    file: str | None = None
    plate: int | None = None
    whole_file: bool = False  # plate unknown: the total of the file is used
    # (material, color, filament id, grams at 100 %)
    usage: list[tuple[str, str, str, float]] | None = None
    progress: float = 0.0
    # Progress counts only after 'running': before that it is stale.
    running: bool = False

    @property
    def id(self) -> str:
        return f"{self.serial}|{int(self.started)}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        usage = data.get("usage")
        return cls(
            serial=str(data["serial"]),
            started=float(data["started"]),
            names=[str(n) for n in data.get("names", [])],
            subtask=str(data.get("subtask", "")),
            file=data.get("file"),
            plate=data.get("plate"),
            whole_file=bool(data.get("whole_file", False)),
            # Jobs stored before the filament id have three fields.
            usage=[
                (str(u[0]), str(u[1]), str(u[2]) if len(u) == 4 else "", float(u[-1]))
                for u in usage
            ]
            if usage is not None
            else None,
            progress=float(data.get("progress", 0.0)),
            running=bool(data.get("running", False)),
        )


def snapshot(parsed: ParsedFile, plate: int | None) -> tuple[list, int | None, bool]:
    """Grams per color for the printed plate, or the whole file if unknown."""
    if plate is not None and parsed.plate(plate) is not None:
        chosen: int | None = plate
    elif len(parsed.plates) == 1:
        chosen = parsed.plates[0].index
    else:
        chosen = None
    usage = parsed.usage(chosen)
    return [(m, c, f, g) for (m, c, f), g in usage.items()], chosen, chosen is None


class PrintTracker:
    """Book filament per finished or failed print."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, manager: StockManager
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._manager = manager
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.tracker"
        )
        self.printers: dict[str, dict[str, str]] = {}  # serial -> key -> entity_id
        self.jobs: dict[str, Job] = {}
        self.last: dict[str, Any] = {}
        self._unsub_states: CALLBACK_TYPE | None = None
        # Status events arrive fast; handle them one at a time, in order.
        self._lock = asyncio.Lock()

    # Storage

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        for serial, data in (stored.get("jobs") or {}).items():
            try:
                self.jobs[serial] = Job.from_dict(data)
            except KeyError, TypeError, ValueError:
                continue
        self.last = dict(stored.get("last") or {})

    def _to_store(self) -> dict[str, Any]:
        return {
            "jobs": {s: asdict(j) for s, j in self.jobs.items()},
            "last": self.last,
        }

    async def async_flush(self) -> None:
        await self._store.async_save(self._to_store())

    @callback
    def _save_soon(self) -> None:
        self._store.async_delay_save(self._to_store, 1)

    # Discovery

    @callback
    def async_start(self) -> list[CALLBACK_TYPE]:
        """Find ha-bambulab sensors, follow them and handle a restart."""
        self._discover()
        unsubs = [
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, self._on_registry
            ),
            self._stop_tracking,
        ]
        for serial in list(self.printers):
            state = self._state(serial, "print_status")
            self._task(self._async_status(serial, state))
        return unsubs

    def _task(self, coro: Any) -> None:
        """Run through the entry: an unload waits for a booking."""
        self._entry.async_create_task(self.hass, coro)

    @callback
    def _stop_tracking(self) -> None:
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None

    def _discover(self) -> None:
        registry = er.async_get(self.hass)
        printers: dict[str, dict[str, str]] = {}
        for entry in registry.entities.values():
            if entry.platform != BAMBU_DOMAIN or entry.domain != "sensor":
                continue
            uid = entry.unique_id
            # Printer sensors are '{serial}_{key}'. AMS and spool sensors differ.
            if "_AMS_" in uid or "ExternalSpool" in uid:
                continue
            for key in KEYS:
                if uid.endswith(f"_{key}"):
                    printers.setdefault(uid[: -len(key) - 1], {})[key] = entry.entity_id
        self.printers = {s: k for s, k in printers.items() if "print_status" in k}
        self._stop_tracking()
        entity_ids = [e for keys in self.printers.values() for e in keys.values()]
        if entity_ids:
            self._unsub_states = async_track_state_change_event(
                self.hass, entity_ids, self._on_state
            )
        elif not self.last:
            self.last = {"result": "no_printer"}
        if self.printers and self.last.get("result") == "no_printer":
            self.last = {}

    @callback
    def _on_registry(self, event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        if event.data["action"] != "create":
            return
        entry = er.async_get(self.hass).async_get(event.data["entity_id"])
        if entry is not None and entry.platform == BAMBU_DOMAIN:
            known = set(self.printers)
            self._discover()
            for serial in set(self.printers) - known:
                state = self._state(serial, "print_status")
                self._task(self._async_status(serial, state))

    # State handling

    def _state(self, serial: str, key: str) -> str | None:
        entity_id = self.printers.get(serial, {}).get(key)
        state = self.hass.states.get(entity_id) if entity_id else None
        return state.state if state else None

    def _names(self, serial: str) -> list[str]:
        names = [self._state(serial, k) for k in ("subtask_name", "gcode_file")]
        return [n for n in names if n and n.lower() not in NO_NAME]

    def _progress(self, serial: str) -> float:
        try:
            return float(self._state(serial, "print_progress") or 0)
        except ValueError:
            return 0.0

    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data.get("new_state")
        for serial, keys in self.printers.items():
            if entity_id == keys.get("print_status"):
                status = new.state if new else None
                self._task(self._async_status(serial, status))
                return
            if entity_id in keys.values():
                self._on_detail(serial)
                return

    def _subtask(self, serial: str) -> str:
        name = self._state(serial, "subtask_name") or ""
        return "" if name.lower() in NO_NAME else normalize_name(name)

    @callback
    def _on_detail(self, serial: str) -> None:
        job = self.jobs.get(serial)
        if job is None:
            return
        subtask = self._subtask(serial)
        if job.subtask and subtask and subtask != job.subtask:
            # Another print is running now: let the status logic decide.
            status = self._state(serial, "print_status")
            self._task(self._async_status(serial, status))
            return
        job.subtask = job.subtask or subtask
        if job.running:
            job.progress = max(job.progress, self._progress(serial))
        for name in self._names(serial):
            if name not in job.names:
                job.names.append(name)
        if job.usage is None:
            self._match(job)
        self._save_soon()

    def _match(self, job: Job) -> None:
        # subtask_name first; 'plate_1.gcode' paths only give the plate.
        candidates = sorted(
            (n for n in job.names if plate_from_gcode(n) is None),
            key=lambda n: normalize_name(n) != job.subtask,
        )
        hit = next(
            (h for n in candidates if (h := self._manager.queue.find([n]))),
            None,
        )
        if hit is None:
            return
        job.file, parsed = hit
        plate = next(
            (p for p in map(plate_from_gcode, job.names) if p is not None), None
        )
        job.usage, job.plate, job.whole_file = snapshot(parsed, plate)

    async def _async_status(self, serial: str, status: str | None) -> None:
        async with self._lock:
            await self._async_handle(serial, status)

    async def _async_handle(self, serial: str, status: str | None) -> None:
        job = self.jobs.get(serial)
        if status in ACTIVE:
            names = self._names(serial)
            subtask = self._subtask(serial)
            if job is not None and job.subtask and subtask and subtask != job.subtask:
                # A new print started while the old one never ended: book nothing.
                self._set_last(job, "missed_end")
                del self.jobs[serial]
                job = None
            if job is None:
                job = Job(
                    serial=serial, started=time.time(), names=names, subtask=subtask
                )
                self.jobs[serial] = job
                self._match(job)
                await self.async_flush()
                self._task(self._manager.spools.async_request_refresh())
            if status == "running":
                job.running = True
            self._on_detail(serial)
        elif status in ("finish", "failed") and job is not None:
            subtask = self._subtask(serial)
            if job.subtask and subtask and subtask != job.subtask:
                # The printer reports the end of another print.
                self._set_last(job, "missed_end", status=status)
                del self.jobs[serial]
                await self.async_flush()
                self._manager.async_update()
            elif status == "finish":
                await self._async_book(serial, job, 1.0, status)
            else:
                progress = job.progress
                if job.running:
                    progress = max(progress, self._progress(serial))
                await self._async_book(serial, job, progress / 100, status)
        elif status in STOPPED and job is not None:
            self._set_last(job, "stopped")
            del self.jobs[serial]
            await self.async_flush()
            self._manager.async_update()
        elif status not in HOLD | STOPPED | {"finish", "failed", None}:
            _LOGGER.debug("Unknown print status %r, job kept", status)

    async def _async_book(
        self, serial: str, job: Job, fraction: float, status: str
    ) -> None:
        if job.usage is None:
            job.names.extend(n for n in self._names(serial) if n not in job.names)
            self._match(job)
        del self.jobs[serial]
        if job.usage is None:
            _LOGGER.warning("No sliced file matches this print, nothing deducted")
            self._set_last(job, "no_match", status=status)
        else:
            fraction = max(0.0, min(1.0, fraction))
            usage: dict[UsageKey, float] = {}
            for material, color, filament_id, grams in job.usage:
                key = (material, color, filament_id)
                usage[key] = usage.get(key, 0.0) + grams * fraction
            result, mapped = await self._manager.async_book(job.id, usage)
            self._set_last(
                job,
                "duplicate" if result.duplicate else "booked",
                status=status,
                fraction=round(fraction, 3),
                grams={
                    self._manager.group_title(k): round(v, 2)
                    for k, v in mapped.grams.items()
                },
                groups={k: round(v, 2) for k, v in mapped.grams.items()},
                # Two loaded spools with the same color: the file cannot tell
                # which one was used. Choose it with the 'in files' select.
                ambiguous={
                    f"{m.material} {m.color}": list(m.candidates)
                    for m in mapped.matches.values()
                    if len(m.candidates) > 1
                },
                spools={str(k): round(v, 2) for k, v in result.per_spool.items()},
                shortage={k: round(v, 2) for k, v in result.shortage.items()},
            )
        await self.async_flush()
        self._task(self._manager.spools.async_request_refresh())
        self._manager.async_update()

    def _set_last(self, job: Job, result: str, **extra: Any) -> None:
        self.last = {
            "result": result,
            "file": job.file,
            "names": job.names,
            "plate": job.plate,
            "whole_file": job.whole_file,
            "time": dt_util.utcnow().isoformat(),
            **extra,
        }
