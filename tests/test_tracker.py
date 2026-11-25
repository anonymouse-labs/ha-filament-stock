"""Tests for the print tracker: every status transition and the acceptance cases."""

from __future__ import annotations

from typing import Any

from conftest import FakeDrive, FakeTransport
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from threemf import REFERENCE_PLATE, build_3mf, slice_info

from custom_components.filament_stock.bambu_api import FILAMENT_PATH
from custom_components.filament_stock.const import DOMAIN
from custom_components.filament_stock.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.filament_stock.parser import parse_3mf
from custom_components.filament_stock.tracker import plate_from_gcode, snapshot

SERIAL = "TESTSERIAL0001"
KEYS = ("print_status", "print_progress", "gcode_file", "subtask_name")
SPOOLS = [
    {"id": 1, "color": "#000000FF", "filamentType": "PLA", "netWeight": 1000,
     "totalNetWeight": 1000},
    {"id": 2, "color": "#898989FF", "filamentType": "PLA", "netWeight": 1000,
     "totalNetWeight": 1000},
]  # fmt: skip
NAME = "Gear box v2"
FILE = f"{NAME}.gcode.3mf"


def _set(hass: HomeAssistant, key: str, value: Any) -> None:
    hass.states.async_set(f"sensor.a1_{key}", str(value))


@pytest.fixture
def printer(hass: HomeAssistant) -> None:
    """A ha-bambulab printer with its sensors in the registry, idle."""
    registry = er.async_get(hass)
    for key in KEYS:
        registry.async_get_or_create(
            "sensor", "bambu_lab", f"{SERIAL}_{key}", suggested_object_id=f"a1_{key}"
        )
    # AMS sensors must be ignored.
    registry.async_get_or_create(
        "sensor",
        "bambu_lab",
        f"A1_{SERIAL}_AMS_X_print_status",
        suggested_object_id="x",
    )
    _set(hass, "print_status", "idle")
    _set(hass, "print_progress", 0)


@pytest.fixture
def setup_data(bambu: FakeTransport, drive: FakeDrive) -> FakeDrive:
    bambu.json("GET", FILAMENT_PATH, {"total": 2, "hits": SPOOLS})
    drive.add("f1", FILE, build_3mf(slice_info([REFERENCE_PLATE])))
    return drive


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _status(hass: HomeAssistant, value: str) -> None:
    _set(hass, "print_status", value)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _start(hass: HomeAssistant, name: str = NAME, plate: int = 1) -> None:
    _set(hass, "subtask_name", name)
    _set(hass, "gcode_file", f"/data/Metadata/plate_{plate}.gcode")
    await _status(hass, "prepare")
    await _status(hass, "running")


def _remaining(entry: MockConfigEntry) -> dict[int, float]:
    ledger = entry.runtime_data.manager.ledger
    return {sid: round(ledger.remaining(sid), 2) for sid in ledger.spools}


def _last(entry: MockConfigEntry) -> dict[str, Any]:
    return entry.runtime_data.tracker.last


pytestmark = pytest.mark.usefixtures("printer", "setup_data")


async def test_finish_books_reference_plate_once(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    assert entry.runtime_data.tracker.jobs[SERIAL].file == FILE
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}
    last = _last(entry)
    assert last["result"] == "booked"
    assert last["fraction"] == 1.0
    # Named like the stock list (the color group), not like the file color.
    assert last["grams"] == {"PLA #000000": 15.02, "PLA #898989": 11.12}
    assert last["spools"] == {"1": 15.02, "2": 11.12}

    # The same end again books nothing extra.
    await _status(hass, "unavailable")
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}
    state = hass.states.get(
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_last_deduction"
        )
    )
    assert state.state == "booked"


async def test_failed_books_progress(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    _set(hass, "print_progress", 40)
    await hass.async_block_till_done()
    _set(hass, "print_progress", 0)  # some printers reset progress on failure
    await hass.async_block_till_done()
    await _status(hass, "failed")
    assert _remaining(entry) == {1: pytest.approx(993.99), 2: pytest.approx(995.55)}
    assert _last(entry)["fraction"] == 0.4


@pytest.mark.parametrize("stop", ["idle", "init"])
async def test_stop_without_end_books_nothing(
    hass: HomeAssistant, entry: MockConfigEntry, stop: str
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, stop)
    assert _remaining(entry) == {1: 1000, 2: 1000}
    assert _last(entry)["result"] == "stopped"
    assert entry.runtime_data.tracker.jobs == {}


@pytest.mark.parametrize("gap", ["unavailable", "offline", "unknown", "weird_state"])
async def test_printer_gap_keeps_job(
    hass: HomeAssistant, entry: MockConfigEntry, gap: str
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, gap)
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_pause_keeps_job(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, "pause")
    await _status(hass, "running")
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_restart_mid_print(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    await _start(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await _setup(hass, entry)
    assert SERIAL in entry.runtime_data.tracker.jobs
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_finish_while_home_assistant_was_down(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _set(hass, "print_status", "finish")

    await _setup(hass, entry)
    assert _remaining(entry) == {1: 984.98, 2: 988.88}
    assert entry.runtime_data.tracker.jobs == {}


async def test_file_moved_after_start(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    setup_data.listing.clear()
    await entry.runtime_data.queue.async_refresh()
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_file_moved_before_start_is_remembered(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    await _setup(hass, entry)
    setup_data.listing.clear()
    await entry.runtime_data.queue.async_refresh()
    await _start(hass)
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_no_matching_file(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    await _start(hass, name="something else")
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 1000, 2: 1000}
    assert _last(entry)["result"] == "no_match"
    assert "something else" in _last(entry)["names"]


async def test_name_arrives_after_start(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _status(hass, "running")
    _set(hass, "subtask_name", NAME)
    await hass.async_block_till_done()
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_second_plate_only(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    two = build_3mf(slice_info([REFERENCE_PLATE, [("PLA", "#898989", 5.0)]]))
    setup_data.add("f2", "Two plates.gcode.3mf", two)
    await _setup(hass, entry)
    await _start(hass, name="Two plates", plate=2)
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 1000, 2: 995}
    assert _last(entry)["plate"] == 2
    assert _last(entry)["whole_file"] is False


async def test_new_print_after_missed_end(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    other = build_3mf(slice_info([[("PLA", "#898989", 3)]]))
    setup_data.add("f2", "Other.gcode.3mf", other)
    await _setup(hass, entry)
    await _start(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # While HA was down the first print ended and another one started.
    _set(hass, "subtask_name", "Other")

    await _setup(hass, entry)
    assert _last(entry)["result"] == "missed_end"
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 1000, 2: 997}


async def test_same_plate_file_name_is_not_a_new_print(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    _set(hass, "gcode_file", "/data/Metadata/plate_1.gcode")
    await _status(hass, "running")
    await _status(hass, "finish")
    assert _last(entry)["result"] == "booked"


async def test_subtask_change_while_running(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    other = build_3mf(slice_info([[("PLA", "#898989", 3)]]))
    setup_data.add("f2", "Other.gcode.3mf", other)
    await _setup(hass, entry)
    await _start(hass)
    _set(hass, "subtask_name", "Other")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _last(entry)["result"] == "missed_end"
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 1000, 2: 997}


async def test_no_printer(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    for reg_entry in list(registry.entities.values()):
        if reg_entry.platform == "bambu_lab":
            registry.async_remove(reg_entry.entity_id)
    await _setup(hass, entry)
    assert _last(entry)["result"] == "no_printer"


def test_plate_from_gcode() -> None:
    assert plate_from_gcode("/data/Metadata/plate_3.gcode") == 3
    assert plate_from_gcode("Metadata/Plate_12.gcode") == 12
    assert plate_from_gcode("Gear box.gcode.3mf") is None
    assert plate_from_gcode(None) is None


def test_snapshot_unknown_plate_uses_whole_file() -> None:
    parsed = parse_3mf(build_3mf(slice_info([REFERENCE_PLATE, REFERENCE_PLATE])))
    usage, plate, whole = snapshot(parsed, None)
    assert plate is None
    assert whole
    assert dict(((m, c), g) for m, c, g in usage)[("PLA", "#898989")] == pytest.approx(
        22.24
    )
    usage, plate, whole = snapshot(parsed, 9)
    assert whole


async def test_printer_added_later(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    for reg_entry in list(registry.entities.values()):
        if reg_entry.platform == "bambu_lab":
            registry.async_remove(reg_entry.entity_id)
    for key in KEYS:
        hass.states.async_remove(f"sensor.a1_{key}")
    await _setup(hass, entry)
    assert entry.runtime_data.tracker.printers == {}
    assert _last(entry)["result"] == "no_printer"

    for key in KEYS:
        registry.async_get_or_create(
            "sensor", "bambu_lab", f"{SERIAL}_{key}", suggested_object_id=f"a1_{key}"
        )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert SERIAL in entry.runtime_data.tracker.printers
    assert _last(entry) == {}
    await _start(hass)
    await _status(hass, "finish")
    assert _remaining(entry) == {1: 984.98, 2: 988.88}


async def test_failure_during_prepare_ignores_old_progress(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    _set(hass, "print_progress", 100)  # left over from the previous print
    _set(hass, "subtask_name", NAME)
    await _status(hass, "prepare")
    await _status(hass, "failed")
    assert _remaining(entry) == {1: 1000, 2: 1000}
    assert _last(entry)["fraction"] == 0


async def test_progress_counts_after_running(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    _set(hass, "print_progress", 100)
    _set(hass, "subtask_name", NAME)
    await _status(hass, "prepare")
    _set(hass, "print_progress", 0)
    await _status(hass, "running")
    _set(hass, "print_progress", 30)
    await hass.async_block_till_done()
    await _status(hass, "failed")
    assert _last(entry)["fraction"] == 0.3


async def test_end_of_another_print_books_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _set(hass, "subtask_name", "Something else")
    _set(hass, "print_status", "finish")

    await _setup(hass, entry)
    assert _last(entry)["result"] == "missed_end"
    assert _remaining(entry) == {1: 1000, 2: 1000}


async def test_subtask_beats_queue_file_named_like_a_plate(
    hass: HomeAssistant, entry: MockConfigEntry, setup_data: FakeDrive
) -> None:
    decoy = build_3mf(slice_info([[("PLA", "#898989", 50)]]))
    setup_data.add("f9", "Plate 1.gcode.3mf", decoy)
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, "finish")
    assert _last(entry)["file"] == FILE


async def test_diagnostics_has_no_serial(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    await _start(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert SERIAL not in str(diag)
    assert diag["tracker"]["jobs"][0]["subtask"] == "gearboxv2"
    state = hass.states.get(
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_last_deduction"
        )
    )
    assert state.attributes["printing"] is True


def _cloud(spool_id: int, color: str, note: str, slot: int | None = None) -> dict:
    """Spool as the cloud returns it; slot is the AMS slot when loaded."""
    loaded = slot is not None
    return {
        "id": spool_id,
        "color": f"{color}FF",
        "filamentType": "PLA",
        "note": note,
        "netWeight": 1000,
        "totalNetWeight": 1000,
        "inPrinter": loaded,
        "amsId": 0 if loaded else None,
        "slotId": str(slot) if loaded else "",
    }


def _two_color_print(
    setup_data: FakeDrive, bambu: FakeTransport, *spools: dict
) -> None:
    bambu.routes.pop(("GET", FILAMENT_PATH), None)
    bambu.json("GET", FILAMENT_PATH, {"total": len(spools), "hits": list(spools)})
    setup_data.listing.clear()
    plate = [("PLA", "#101410", 0.76), ("PLA", "#FFFFFF", 188.66)]
    setup_data.add("f1", FILE, build_3mf(slice_info([plate])))


async def test_books_the_loaded_spools_not_lookalikes(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    setup_data: FakeDrive,
) -> None:
    """Real setup: black and white in the AMS, a lookalike black and a glossy
    white with the exact same hex on the shelf."""
    _two_color_print(
        setup_data,
        bambu,
        _cloud(10, "#101410", "black", slot=0),
        _cloud(11, "#FFFFFF", "white matte", slot=3),
        _cloud(12, "#000000", "black shelf"),
        _cloud(13, "#FFFFFF", "white glossy"),
    )
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, "finish")
    assert _remaining(entry) == {10: 999.24, 11: 811.34, 12: 1000, 13: 1000}
    assert _last(entry)["ambiguous"] == {}


async def test_two_loaded_spools_with_the_same_color_are_flagged(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    setup_data: FakeDrive,
) -> None:
    _two_color_print(
        setup_data,
        bambu,
        _cloud(10, "#101410", "black", slot=0),
        _cloud(11, "#FFFFFF", "white matte", slot=2),
        _cloud(12, "#FFFFFF", "white glossy", slot=3),
    )
    await _setup(hass, entry)
    await _start(hass)
    await _status(hass, "finish")
    flagged = _last(entry)["ambiguous"]
    assert list(flagged) == ["PLA #FFFFFF"]
    assert len(flagged["PLA #FFFFFF"]) == 2
