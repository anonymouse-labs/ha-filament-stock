"""Match a file color (material, hex) to a color group.

Pure Python, no Home Assistant imports. Order:
1. A fixed mapping set by the user: for the color and filament profile id,
   else for the color.
2. Exact material and hex. Several groups (same hex, other note): the group
   whose spools have the file's filament profile id, then the group with a
   spool in the printer, else the group with the most stock.
3. Optional: nearest group of the same material within a CIEDE2000 distance.
4. No match: an 'unknown' group. Never silently ignored.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .colors import color_distance
from .models import ColorGroup, GroupKey
from .parser import UsageKey, normalize_material

DEFAULT_MAX_DISTANCE = 10.0


class MatchMethod(StrEnum):
    """How a file color was matched."""

    FIXED = "fixed"
    EXACT = "exact"
    FILAMENT_ID = "filament_id"
    IN_PRINTER = "in_printer"
    MOST_STOCK = "most_stock"
    NEAREST = "nearest"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Match:
    """Result for one file color."""

    material: str
    color: str
    group_id: str  # an 'unknown_*' id when method is NONE
    method: MatchMethod
    distance: float | None = None
    candidates: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.method is not MatchMethod.NONE


@dataclass(slots=True)
class Needs:
    """Queue needs mapped to groups."""

    grams: dict[str, float] = field(default_factory=dict)  # group id -> grams
    matches: dict[UsageKey, Match] = field(default_factory=dict)

    @property
    def unmatched(self) -> list[Match]:
        return [m for m in self.matches.values() if not m.matched]


def unknown_group_id(material: str, color: str) -> str:
    return "unknown_" + GroupKey.of(material, color, "").id


def _by_filament_id(
    group_ids: Iterable[str], filament_id: str, groups: Mapping[str, ColorGroup]
) -> list[str]:
    """The groups with a spool of this profile id; all of them when none has it."""
    ids = list(group_ids)
    same = [g for g in ids if filament_id and filament_id in groups[g].filament_ids]
    return same or ids


def match_color(
    material: str,
    color: str,
    groups: Mapping[str, ColorGroup],
    stock: Mapping[str, float],
    *,
    filament_id: str = "",
    fixed: Mapping[str, str] | None = None,
    nearest: bool = True,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    prefer_loaded: bool = False,
) -> Match:
    """Match one file color. `fixed` maps a `fixed_key` to a group id.

    With `prefer_loaded` (used when booking a print) the groups with a spool in
    the printer are tried first: the print used what was loaded. The
    `filament_id` from the file only chooses between groups of the same color.
    """
    material = normalize_material(material)
    color = color.upper()

    for key in (fixed_key(material, color, filament_id), fixed_key(material, color)):
        target = (fixed or {}).get(key)
        if target is not None and target in groups:
            return Match(material, color, target, MatchMethod.FIXED)

    same_material = [g for g in groups.values() if g.material == material]
    if prefer_loaded:
        scored_loaded = sorted(
            (color_distance(color, g.color), g.id)
            for g in same_material
            if g.in_printer
        )
        if scored_loaded and scored_loaded[0][0] <= max_distance:
            distance = scored_loaded[0][0]
            # Loaded groups at the same distance (same color, other note, e.g.
            # matte and glossy): the profile id may tell them apart, else flag them.
            ties = _by_filament_id(
                (gid for d, gid in scored_loaded if d - distance < 0.01),
                filament_id,
                groups,
            )
            return Match(
                material,
                color,
                ties[0],
                MatchMethod.IN_PRINTER,
                round(distance, 2),
                tuple(ties) if len(ties) > 1 else (),
            )
    exact = sorted((g for g in same_material if g.color == color), key=lambda g: g.id)
    if len(exact) == 1:
        return Match(material, color, exact[0].id, MatchMethod.EXACT)
    if exact:
        ids = tuple(g.id for g in exact)
        same_id = _by_filament_id(ids, filament_id, groups)
        if len(same_id) == 1:
            return Match(material, color, same_id[0], MatchMethod.FILAMENT_ID)
        exact = [groups[g] for g in same_id]
        loaded = [g for g in exact if g.in_printer]
        if len(loaded) == 1:
            return Match(
                material, color, loaded[0].id, MatchMethod.IN_PRINTER, None, ids
            )
        pool = loaded or exact
        best = max(pool, key=lambda g: stock.get(g.id, 0.0))
        return Match(material, color, best.id, MatchMethod.MOST_STOCK, None, ids)

    if nearest and same_material:
        scored = sorted(
            (color_distance(color, g.color), -stock.get(g.id, 0.0), g.id)
            for g in same_material
        )
        distance, _, group_id = scored[0]
        if distance <= max_distance:
            return Match(
                material, color, group_id, MatchMethod.NEAREST, round(distance, 2)
            )

    return Match(material, color, unknown_group_id(material, color), MatchMethod.NONE)


def fixed_key(material: str, color: str, filament_id: str = "") -> str:
    """'MATERIAL|#RRGGBB', with '|<filament id>' for one filament profile."""
    key = f"{normalize_material(material)}|{color.upper()}"
    return f"{key}|{filament_id}" if filament_id else key


def map_needs(
    usage: Mapping[UsageKey, float],
    groups: Mapping[str, ColorGroup],
    stock: Mapping[str, float],
    *,
    fixed: Mapping[str, str] | None = None,
    nearest: bool = True,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    prefer_loaded: bool = False,
) -> Needs:
    """Map grams per (material, color, filament id) to grams per group."""
    needs = Needs()
    for (material, color, filament_id), grams in usage.items():
        match = match_color(
            material,
            color,
            groups,
            stock,
            filament_id=filament_id,
            fixed=fixed,
            nearest=nearest,
            max_distance=max_distance,
            prefer_loaded=prefer_loaded,
        )
        needs.matches[(match.material, match.color, filament_id)] = match
        needs.grams[match.group_id] = needs.grams.get(match.group_id, 0.0) + grams
    return needs


def nearest_group(
    material: str, color: str, groups: Mapping[str, ColorGroup]
) -> tuple[str, float] | None:
    """Closest group of the same material, at any distance. For suggestions."""
    material = normalize_material(material)
    scored = sorted(
        (color_distance(color, g.color), g.id)
        for g in groups.values()
        if g.material == material
    )
    return (scored[0][1], round(scored[0][0], 1)) if scored else None
