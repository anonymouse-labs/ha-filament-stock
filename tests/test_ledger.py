"""Tests for the spool ledger."""

import pytest

from custom_components.filament_stock.ledger import MAX_APPLIED_JOBS, Ledger
from custom_components.filament_stock.models import Spool, build_groups


def spool(sid: int, color: str, net: float, note: str = "", **kw) -> Spool:
    return Spool(
        id=sid,
        material="PLA",
        color=color,
        note=note,
        net_weight=net,
        total_weight=1000.0,
        **kw,
    )


def test_first_sync_marks_all_new() -> None:
    ledger = Ledger()
    result = ledger.sync([spool(1, "#000000", 1000), spool(2, "#898989", 500)])
    assert result.first
    assert sorted(result.new) == [1, 2]
    again = ledger.sync([spool(1, "#000000", 1000), spool(2, "#898989", 500)])
    assert not again.first
    assert again.new == []
    assert again.rebased == []


def test_new_and_removed_spools() -> None:
    ledger = Ledger()
    ledger.sync([spool(1, "#000000", 1000)])
    result = ledger.sync([spool(2, "#000000", 1000)])
    assert result.new == [2]
    assert result.removed == [1]
    assert list(ledger.spools) == [2]


def test_reference_print_deducts_exact_grams() -> None:
    spools = [spool(1, "#000000", 1000), spool(2, "#898989", 1000)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    result = ledger.deduct("job-1", {"pla_000000": 15.02, "pla_898989": 11.12}, groups)
    assert not result.duplicate
    assert result.per_spool == {1: pytest.approx(15.02), 2: pytest.approx(11.12)}
    assert result.shortage == {}
    stock = ledger.stock(groups)
    assert stock["pla_000000"] == pytest.approx(984.98)
    assert stock["pla_898989"] == pytest.approx(988.88)


def test_same_job_is_booked_once() -> None:
    spools = [spool(1, "#000000", 1000)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 10}, groups)
    again = ledger.deduct("job-1", {"pla_000000": 10}, groups)
    assert again.duplicate
    assert again.per_spool == {}
    assert ledger.stock(groups)["pla_000000"] == pytest.approx(990)


def test_unchanged_cloud_weight_keeps_local_use() -> None:
    spools = [spool(1, "#000000", 1000, updated_at=1)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 100}, groups)
    # Note edited in Studio: updatedAt changes, netWeight does not.
    result = ledger.sync([spool(1, "#000000", 1000, updated_at=2)])
    assert result.rebased == []
    assert ledger.remaining(1) == pytest.approx(900)


def test_changed_cloud_weight_rebases() -> None:
    spools = [spool(1, "#000000", 1000)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 100}, groups)
    # Weight written back or corrected in Studio: no double deduction.
    result = ledger.sync([spool(1, "#000000", 900)])
    assert result.rebased == [1]
    assert ledger.remaining(1) == pytest.approx(900)


def test_in_printer_spool_goes_first() -> None:
    spools = [
        spool(1, "#000000", 200),
        spool(2, "#000000", 1000, in_printer=True),
    ]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    result = ledger.deduct("job-1", {"pla_000000": 50}, groups)
    assert result.per_spool == {2: pytest.approx(50)}


def test_lowest_remaining_goes_first_then_overflow() -> None:
    spools = [
        spool(1, "#000000", 1000),
        spool(2, "#000000", 30),
        spool(3, "#000000", 500),
    ]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    result = ledger.deduct("job-1", {"pla_000000": 100}, groups)
    assert result.per_spool == {2: pytest.approx(30), 3: pytest.approx(70)}
    assert ledger.remaining(2) == 0
    assert ledger.remaining(3) == pytest.approx(430)


def test_empty_group_books_shortage() -> None:
    spools = [spool(1, "#000000", 40)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    result = ledger.deduct("job-1", {"pla_000000": 50}, groups)
    assert result.per_spool == {1: pytest.approx(40)}
    assert result.shortage == {"pla_000000": pytest.approx(10)}
    assert ledger.stock(groups)["pla_000000"] == pytest.approx(-10)


def test_unknown_group_goes_negative() -> None:
    ledger = Ledger()
    result = ledger.deduct("job-1", {"unknown_pla_123456": 7.5}, {})
    assert result.shortage == {"unknown_pla_123456": 7.5}
    assert ledger.stock({}) == {"unknown_pla_123456": -7.5}


def test_rebase_clears_group_shortage() -> None:
    spools = [spool(1, "#000000", 10)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 50}, groups)
    ledger.sync([spool(1, "#000000", 800)])
    assert ledger.stock(groups)["pla_000000"] == pytest.approx(800)


def test_notes_keep_stock_apart() -> None:
    spools = [spool(1, "#006633", 1000, "mat"), spool(2, "#006633", 300, "glans")]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    stock = ledger.stock(groups)
    assert sorted(stock.values()) == [300, 1000]


def test_clear_shortage() -> None:
    ledger = Ledger()
    ledger.deduct("job-1", {"unknown_pla_123456": 7.5}, {})
    assert ledger.clear_shortage("unknown_pla_123456")
    assert ledger.stock({}) == {}
    assert not ledger.clear_shortage("unknown_pla_123456")


def test_reset_spool() -> None:
    spools = [spool(1, "#000000", 1000)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 100}, groups)
    assert ledger.reset_spool(1)
    assert ledger.remaining(1) == 1000
    assert not ledger.reset_spool(99)


def test_applied_jobs_are_bounded() -> None:
    ledger = Ledger()
    for i in range(MAX_APPLIED_JOBS + 10):
        ledger.deduct(f"job-{i}", {}, {})
    assert len(ledger.applied) == MAX_APPLIED_JOBS
    assert ledger.applied[-1] == f"job-{MAX_APPLIED_JOBS + 9}"


def test_round_trip_dict() -> None:
    spools = [spool(1, "#000000", 40)]
    groups = build_groups(spools)
    ledger = Ledger()
    ledger.sync(spools)
    ledger.deduct("job-1", {"pla_000000": 50}, groups)
    restored = Ledger.from_dict(ledger.as_dict())
    assert restored == ledger
    assert Ledger.from_dict(None) == Ledger()
