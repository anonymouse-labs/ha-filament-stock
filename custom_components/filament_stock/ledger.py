"""Local ledger of filament used per spool since the last known cloud weight.

Pure Python, no Home Assistant imports.

Effective remaining per spool = cloud netWeight at the last rebase - local use.
When the cloud netWeight changes (edited in Bambu Studio, or written back),
the spool is rebased: the new cloud value already includes earlier use.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .models import ColorGroup, Spool

MAX_APPLIED_JOBS = 100
_EPSILON = 1e-6


@dataclass(slots=True)
class SpoolEntry:
    """Ledger state for one spool."""

    base: float  # cloud netWeight at the last rebase
    used: float = 0.0  # grams used since then

    @property
    def remaining(self) -> float:
        return max(0.0, self.base - self.used)


@dataclass(slots=True)
class SyncResult:
    """What changed on a cloud sync."""

    first: bool = False
    new: list[int] = field(default_factory=list)
    rebased: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)


@dataclass(slots=True)
class Deduction:
    """Result of booking one print."""

    job_id: str
    duplicate: bool = False
    per_spool: dict[int, float] = field(default_factory=dict)
    shortage: dict[str, float] = field(default_factory=dict)  # group id -> grams


@dataclass(slots=True)
class Ledger:
    """Per-spool use, per-group shortage and booked print jobs."""

    spools: dict[int, SpoolEntry] = field(default_factory=dict)
    shortage: dict[str, float] = field(default_factory=dict)
    applied: list[str] = field(default_factory=list)
    synced: bool = False

    def sync(self, spools: Iterable[Spool]) -> SyncResult:
        """Align with the cloud list. Only a netWeight change rebases."""
        result = SyncResult(first=not self.synced)
        seen: set[int] = set()
        for spool in spools:
            seen.add(spool.id)
            entry = self.spools.get(spool.id)
            if entry is None:
                self.spools[spool.id] = SpoolEntry(spool.net_weight)
                result.new.append(spool.id)
            elif abs(entry.base - spool.net_weight) > _EPSILON:
                self.spools[spool.id] = SpoolEntry(spool.net_weight)
                # A new weight was entered for this color: the old shortage is void.
                self.shortage.pop(spool.group_key.id, None)
                result.rebased.append(spool.id)
        for spool_id in set(self.spools) - seen:
            del self.spools[spool_id]
            result.removed.append(spool_id)
        self.synced = True
        return result

    def remaining(self, spool_id: int) -> float:
        entry = self.spools.get(spool_id)
        return entry.remaining if entry else 0.0

    def stock(self, groups: Mapping[str, ColorGroup]) -> dict[str, float]:
        """Stock per group id, shortage subtracted. Unknown groups included."""
        stock = {
            gid: sum(self.remaining(s.id) for s in g.spools)
            for gid, g in groups.items()
        }
        for gid, grams in self.shortage.items():
            stock[gid] = stock.get(gid, 0.0) - grams
        return stock

    def deduct(
        self,
        job_id: str,
        grams_by_group: Mapping[str, float],
        groups: Mapping[str, ColorGroup],
    ) -> Deduction:
        """Book a print. In-printer spool first, then the lowest remaining."""
        result = Deduction(job_id)
        if job_id in self.applied:
            result.duplicate = True
            return result

        for group_id, grams in grams_by_group.items():
            left = max(0.0, grams)
            group = groups.get(group_id)
            spools = sorted(
                group.spools if group else [],
                key=lambda s: (not s.in_printer, self.remaining(s.id), s.id),
            )
            for spool in spools:
                if left <= _EPSILON:
                    break
                entry = self.spools.get(spool.id)
                if entry is None or entry.remaining <= _EPSILON:
                    continue
                take = min(entry.remaining, left)
                entry.used += take
                left -= take
                result.per_spool[spool.id] = result.per_spool.get(spool.id, 0.0) + take
            if left > _EPSILON:
                self.shortage[group_id] = self.shortage.get(group_id, 0.0) + left
                result.shortage[group_id] = left

        self.applied.append(job_id)
        del self.applied[:-MAX_APPLIED_JOBS]
        return result

    def reset_spool(self, spool_id: int) -> bool:
        """Forget local use for one spool. Returns False if unknown."""
        entry = self.spools.get(spool_id)
        if entry is None:
            return False
        entry.used = 0.0
        return True

    def clear_shortage(self, group_id: str) -> bool:
        """Forget the shortage of one group. Returns False if there was none."""
        return self.shortage.pop(group_id, None) is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "spools": {str(k): [v.base, v.used] for k, v in self.spools.items()},
            "shortage": dict(self.shortage),
            "applied": list(self.applied),
            "synced": self.synced,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Ledger:
        if not data:
            return cls()
        return cls(
            spools={
                int(k): SpoolEntry(float(base), float(used))
                for k, (base, used) in data.get("spools", {}).items()
            },
            shortage={str(k): float(v) for k, v in data.get("shortage", {}).items()},
            applied=[str(j) for j in data.get("applied", [])][-MAX_APPLIED_JOBS:],
            synced=bool(data.get("synced", False)),
        )
