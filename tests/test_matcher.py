"""Tests for color groups and the matcher."""

import pytest

from custom_components.filament_stock.matcher import (
    MatchMethod,
    fixed_key,
    map_needs,
    match_color,
    unknown_group_id,
)
from custom_components.filament_stock.models import GroupKey, Spool, build_groups


def spool(sid: int, color: str, note: str = "", material: str = "PLA", **kw) -> Spool:
    return Spool(id=sid, material=material, color=color, note=note, **kw)


def test_note_splits_groups() -> None:
    groups = build_groups(
        [
            spool(1, "#006633", "mat"),
            spool(2, "#006633", "hoogglans"),
            spool(3, "#006633", " Mat "),
            spool(4, "#006633"),
        ]
    )
    assert len(groups) == 3
    mat = groups[GroupKey.of("PLA", "#006633", "mat").id]
    assert [s.id for s in mat.spools] == [1, 3]
    assert mat.label == "PLA #006633 mat"
    assert groups[GroupKey.of("PLA", "#006633", "").id].label == "PLA #006633"


def test_group_id_is_stable_and_ascii() -> None:
    key = GroupKey.of("PLA Basic", "#006633", "Mat  zwart")
    assert key == GroupKey.of("pla basic", "#006633", " mat ZWART")
    assert key.id.startswith("pla_basic_006633_")
    assert key.id.isascii()
    assert GroupKey.of("PLA", "#006633", "").id == "pla_006633"


def test_punctuated_material_does_not_collide() -> None:
    groups = build_groups([spool(1, "#FFFFFF"), spool(2, "#FFFFFF", material="PLA+")])
    assert len(groups) == 2
    assert "pla_ffffff" in groups


def test_cloud_color_with_alpha_is_normalized() -> None:
    assert GroupKey.of("PLA", "#ffffffFF", "").id == "pla_ffffff"


def test_exact_match() -> None:
    groups = build_groups([spool(1, "#101410"), spool(2, "#898989")])
    match = match_color("pla", "#898989", groups, {})
    assert match.method is MatchMethod.EXACT
    assert match.group_id == "pla_898989"


def test_material_must_match() -> None:
    groups = build_groups([spool(1, "#898989", material="PETG")])
    match = match_color("PLA", "#898989", groups, {}, nearest=True)
    assert match.method is MatchMethod.NONE
    assert match.group_id == unknown_group_id("PLA", "#898989")


def test_same_hex_other_note_prefers_in_printer() -> None:
    groups = build_groups(
        [spool(1, "#006633", "mat"), spool(2, "#006633", "glans", in_printer=True)]
    )
    stock = {gid: 1000.0 for gid in groups}
    match = match_color("PLA", "#006633", groups, stock)
    assert match.method is MatchMethod.IN_PRINTER
    assert match.group_id == GroupKey.of("PLA", "#006633", "glans").id
    assert len(match.candidates) == 2


def test_same_hex_other_note_falls_back_to_most_stock() -> None:
    groups = build_groups([spool(1, "#006633", "mat"), spool(2, "#006633", "glans")])
    mat = GroupKey.of("PLA", "#006633", "mat").id
    glans = GroupKey.of("PLA", "#006633", "glans").id
    match = match_color("PLA", "#006633", groups, {mat: 100.0, glans: 600.0})
    assert match.method is MatchMethod.MOST_STOCK
    assert match.group_id == glans


def test_both_in_printer_uses_most_stock() -> None:
    groups = build_groups(
        [
            spool(1, "#006633", "mat", in_printer=True),
            spool(2, "#006633", "glans", in_printer=True),
            spool(3, "#006633", "satijn"),
        ]
    )
    satijn = GroupKey.of("PLA", "#006633", "satijn").id
    mat = GroupKey.of("PLA", "#006633", "mat").id
    match = match_color("PLA", "#006633", groups, {satijn: 5000.0, mat: 10.0})
    assert match.method is MatchMethod.MOST_STOCK
    assert match.group_id == mat


def test_fixed_mapping_wins() -> None:
    groups = build_groups([spool(1, "#006633", "mat"), spool(2, "#101410")])
    target = GroupKey.of("PLA", "#006633", "mat").id
    fixed = {fixed_key("pla", "#101410"): target}
    match = match_color("PLA", "#101410", groups, {}, fixed=fixed)
    assert match.method is MatchMethod.FIXED
    assert match.group_id == target


def test_fixed_mapping_to_missing_group_is_ignored() -> None:
    groups = build_groups([spool(1, "#101410")])
    fixed = {fixed_key("PLA", "#101410"): "gone"}
    match = match_color("PLA", "#101410", groups, {}, fixed=fixed)
    assert match.method is MatchMethod.EXACT


def test_nearest_within_threshold() -> None:
    groups = build_groups([spool(1, "#000000"), spool(2, "#FFFFFF")])
    match = match_color("PLA", "#101410", groups, {})
    assert match.method is MatchMethod.NEAREST
    assert match.group_id == "pla_000000"
    assert match.distance is not None
    assert match.distance < 10


def test_nearest_disabled_or_too_far() -> None:
    groups = build_groups([spool(1, "#000000")])
    assert (
        match_color("PLA", "#101410", groups, {}, nearest=False).method
        is MatchMethod.NONE
    )
    assert (
        match_color("PLA", "#101410", groups, {}, max_distance=1).method
        is MatchMethod.NONE
    )


def test_no_groups_is_unknown() -> None:
    match = match_color("PLA", "#101410", {}, {})
    assert not match.matched
    assert match.group_id == "unknown_pla_101410"


def test_map_needs_reference_plate() -> None:
    groups = build_groups([spool(1, "#000000"), spool(2, "#898989")])
    usage = {("PLA", "#101410"): 15.02, ("PLA", "#898989"): 11.12}
    needs = map_needs(usage, groups, {})
    assert needs.grams == {
        "pla_000000": pytest.approx(15.02),
        "pla_898989": pytest.approx(11.12),
    }
    assert needs.unmatched == []
    assert needs.matches[("PLA", "#101410")].method is MatchMethod.NEAREST


def test_map_needs_reports_unmatched() -> None:
    needs = map_needs({("PETG", "#FF0000"): 4.0}, {}, {})
    assert needs.grams == {"unknown_petg_ff0000": 4.0}
    assert [m.color for m in needs.unmatched] == ["#FF0000"]


def test_map_needs_merges_into_one_group() -> None:
    groups = build_groups([spool(1, "#000000")])
    usage = {("PLA", "#000000"): 1.0, ("PLA", "#050505"): 2.0}
    assert map_needs(usage, groups, {}).grams == {"pla_000000": 3.0}
