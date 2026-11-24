"""Match a file color (material, hex) to a color group.

Pure Python, no Home Assistant imports. Order:
1. A fixed mapping set by the user.
2. Exact material and hex. Several groups (same hex, other note): the group
   with a spool in the printer, else the group with the most stock.
3. Optional: nearest group of the same material within a CIEDE2000 distance.
4. No match: an 'unknown' group. Never silently ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .colors import color_distance
from .models import ColorGroup, GroupKey
from .parser import normalize_material

DEFAULT_MAX_DISTANCE = 10.0


class MatchMethod(StrEnum):
    """How a file color was matched."""

    FIXED = "fixed"
    EXACT = "exact"
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
    matches: dict[tuple[str, str], Match] = field(default_factory=dict)

    @property
    def unmatched(self) -> list[Match]:
        return [m for m in self.matches.values() if not m.matched]


def unknown_group_id(material: str, color: str) -> str:
    return "unknown_" + GroupKey.of(material, color, "").id


def match_color(
    material: str,
    color: str,
    groups: Mapping[str, ColorGroup],
    stock: Mapping[str, float],
    *,
    fixed: Mapping[str, str] | None = None,
    nearest: bool = True,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> Match:
    """Match one file color. `fixed` maps 'MATERIAL|#RRGGBB' to a group id."""
    material = normalize_material(material)
    color = color.upper()

    target = (fixed or {}).get(fixed_key(material, color))
    if target is not None and target in groups:
        return Match(material, color, target, MatchMethod.FIXED)

    same_material = [g for g in groups.values() if g.material == material]
    exact = sorted((g for g in same_material if g.color == color), key=lambda g: g.id)
    if len(exact) == 1:
        return Match(material, color, exact[0].id, MatchMethod.EXACT)
    if exact:
        ids = tuple(g.id for g in exact)
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


def fixed_key(material: str, color: str) -> str:
    return f"{normalize_material(material)}|{color.upper()}"


def map_needs(
    usage: Mapping[tuple[str, str], float],
    groups: Mapping[str, ColorGroup],
    stock: Mapping[str, float],
    *,
    fixed: Mapping[str, str] | None = None,
    nearest: bool = True,
    max_distance: float = DEFAULT_MAX_DISTANCE,
) -> Needs:
    """Map grams per (material, color) to grams per group."""
    needs = Needs()
    for (material, color), grams in usage.items():
        match = match_color(
            material,
            color,
            groups,
            stock,
            fixed=fixed,
            nearest=nearest,
            max_distance=max_distance,
        )
        needs.matches[(match.material, match.color)] = match
        needs.grams[match.group_id] = needs.grams.get(match.group_id, 0.0) + grams
    return needs
