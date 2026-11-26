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
    usage = {("PLA", "#101410", ""): 15.02, ("PLA", "#898989", ""): 11.12}
    needs = map_needs(usage, groups, {})
    assert needs.grams == {
        "pla_000000": pytest.approx(15.02),
        "pla_898989": pytest.approx(11.12),
    }
    assert needs.unmatched == []
    assert needs.matches[("PLA", "#101410", "")].method is MatchMethod.NEAREST


def test_map_needs_reports_unmatched() -> None:
    needs = map_needs({("PETG", "#FF0000", ""): 4.0}, {}, {})
    assert needs.grams == {"unknown_petg_ff0000": 4.0}
    assert [m.color for m in needs.unmatched] == ["#FF0000"]


def test_map_needs_merges_into_one_group() -> None:
    groups = build_groups([spool(1, "#000000")])
    usage = {("PLA", "#000000", ""): 1.0, ("PLA", "#050505", ""): 2.0}
    assert map_needs(usage, groups, {}).grams == {"pla_000000": 3.0}


def test_booking_prefers_the_loaded_spool() -> None:
    """Three greys in stock, the file says one grey: the loaded grey is used."""
    groups = build_groups(
        [
            spool(1, "#898989"),
            spool(2, "#808080", in_printer=True),
            spool(3, "#A0A0A0"),
        ]
    )
    planned = match_color("PLA", "#898989", groups, {})
    assert planned.group_id == "pla_898989"
    booked = match_color("PLA", "#898989", groups, {}, prefer_loaded=True)
    assert booked.method is MatchMethod.IN_PRINTER
    assert booked.group_id == "pla_808080"


def test_booking_ignores_a_loaded_spool_of_another_color() -> None:
    groups = build_groups([spool(1, "#898989"), spool(2, "#FF3300", in_printer=True)])
    booked = match_color("PLA", "#898989", groups, {}, prefer_loaded=True)
    assert booked.method is MatchMethod.EXACT
    assert booked.group_id == "pla_898989"


def test_fixed_mapping_beats_loaded_spool() -> None:
    groups = build_groups([spool(1, "#898989"), spool(2, "#808080", in_printer=True)])
    fixed = {fixed_key("PLA", "#898989"): "pla_898989"}
    booked = match_color("PLA", "#898989", groups, {}, fixed=fixed, prefer_loaded=True)
    assert booked.group_id == "pla_898989"


def _whites() -> dict:
    """Cotton White is loaded, Ivory White waits on the shelf: same hex."""
    return build_groups(
        [
            spool(1, "#FFFFFF", "Cotton", in_printer=True, filament_id="GFL99"),
            spool(2, "#FFFFFF", "Ivory", filament_id="GFA01"),
        ]
    )


def test_filament_id_tells_same_colors_apart_in_the_queue() -> None:
    groups = _whites()
    cotton = GroupKey.of("PLA", "#FFFFFF", "Cotton").id
    ivory = GroupKey.of("PLA", "#FFFFFF", "Ivory").id
    usage = {("PLA", "#FFFFFF", "GFL99"): 94.0, ("PLA", "#FFFFFF", "GFA01"): 368.0}
    needs = map_needs(usage, groups, {})
    assert needs.grams == {cotton: 94.0, ivory: 368.0}
    assert needs.matches[("PLA", "#FFFFFF", "GFA01")].method is MatchMethod.FILAMENT_ID
    # Unknown or missing id: the old rules, the loaded group wins.
    for filament_id in ("GFA00", ""):
        match = match_color("PLA", "#FFFFFF", groups, {}, filament_id=filament_id)
        assert match.group_id == cotton


def test_booking_keeps_the_loaded_spool_over_the_filament_id() -> None:
    """Sliced for Ivory but printed with Cotton loaded: Cotton was used."""
    groups = _whites()
    booked = match_color(
        "PLA", "#FFFFFF", groups, {}, filament_id="GFA01", prefer_loaded=True
    )
    assert booked.group_id == GroupKey.of("PLA", "#FFFFFF", "Cotton").id
    assert booked.method is MatchMethod.IN_PRINTER


def test_filament_id_resolves_two_loaded_spools_of_one_color() -> None:
    groups = build_groups(
        [
            spool(1, "#FFFFFF", "Cotton", in_printer=True, filament_id="GFL99"),
            spool(2, "#FFFFFF", "Ivory", in_printer=True, filament_id="GFA01"),
        ]
    )
    booked = match_color(
        "PLA", "#FFFFFF", groups, {}, filament_id="GFA01", prefer_loaded=True
    )
    assert booked.group_id == GroupKey.of("PLA", "#FFFFFF", "Ivory").id
    assert booked.candidates == ()
    unsure = match_color("PLA", "#FFFFFF", groups, {}, prefer_loaded=True)
    assert len(unsure.candidates) == 2


def test_fixed_choice_per_filament_id_beats_one_for_the_color() -> None:
    groups = _whites()
    cotton = GroupKey.of("PLA", "#FFFFFF", "Cotton").id
    ivory = GroupKey.of("PLA", "#FFFFFF", "Ivory").id
    fixed = {
        fixed_key("PLA", "#FFFFFF"): ivory,
        fixed_key("PLA", "#FFFFFF", "GFL99"): cotton,
    }
    assert fixed_key("pla", "#ffffff", "GFL99") == "PLA|#FFFFFF|GFL99"
    for filament_id, expected in (("GFL99", cotton), ("GFA01", ivory), ("", ivory)):
        match = match_color(
            "PLA", "#FFFFFF", groups, {}, filament_id=filament_id, fixed=fixed
        )
        assert (match.group_id, match.method) == (expected, MatchMethod.FIXED)
