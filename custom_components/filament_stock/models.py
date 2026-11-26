"""Spools and color groups.

Pure Python, no Home Assistant imports.

A color group is (material, '#RRGGBB', note). The note is compared trimmed and
case-insensitive, so two spools that differ only in note are two groups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

from .colors import normalize_hex
from .parser import normalize_material


@dataclass(frozen=True, slots=True)
class Spool:
    """One spool from the cloud filament list."""

    id: int
    material: str  # normalized, e.g. 'PLA'
    color: str  # '#RRGGBB'
    note: str = ""
    name: str = ""
    vendor: str = ""
    net_weight: float = 0.0  # remaining grams according to the cloud
    total_weight: float = 0.0  # full spool grams
    in_printer: bool = False
    updated_at: int | None = None
    filament_id: str = ""  # Bambu profile id, e.g. 'GFA01'

    @property
    def group_key(self) -> GroupKey:
        return GroupKey.of(self.material, self.color, self.note)


@dataclass(frozen=True, slots=True)
class GroupKey:
    """Identity of a color group."""

    material: str
    color: str
    note_key: str  # trimmed, casefolded; '' for no note

    @classmethod
    def of(cls, material: str, color: str, note: str | None) -> GroupKey:
        return cls(
            normalize_material(material),
            normalize_hex(color) or "#000000",
            " ".join((note or "").split()).casefold(),
        )

    @property
    def id(self) -> str:
        """Stable ASCII id, safe for unique IDs.

        A hash is added when the note is set or the material slug lost
        characters, so 'PLA' and 'PLA+' never share an id.
        """
        lower = self.material.lower()
        slug = re.sub(r"[^a-z0-9]+", "_", lower).strip("_") or "unknown"
        base = f"{slug}_{self.color[1:].lower()}"
        if not self.note_key and slug == lower:
            return base
        raw = f"{self.material}|{self.note_key}".encode()
        return f"{base}_{hashlib.sha256(raw).hexdigest()[:8]}"


@dataclass(slots=True)
class ColorGroup:
    """Spools sharing material, color and note."""

    key: GroupKey
    note: str  # display form of the note
    spools: list[Spool] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.key.id

    @property
    def material(self) -> str:
        return self.key.material

    @property
    def color(self) -> str:
        return self.key.color

    @property
    def in_printer(self) -> bool:
        return any(s.in_printer for s in self.spools)

    @property
    def filament_ids(self) -> set[str]:
        return {s.filament_id for s in self.spools if s.filament_id}

    @property
    def label(self) -> str:
        """Readable name, e.g. 'PLA #006633 mat'."""
        return " ".join(p for p in (self.material, self.color, self.note) if p)


def build_groups(spools: list[Spool]) -> dict[str, ColorGroup]:
    """Group spools by (material, color, note)."""
    groups: dict[str, ColorGroup] = {}
    for spool in sorted(spools, key=lambda s: s.id):
        key = spool.group_key
        group = groups.get(key.id)
        if group is None:
            group = groups[key.id] = ColorGroup(key, " ".join(spool.note.split()))
        group.spools.append(spool)
    return groups
