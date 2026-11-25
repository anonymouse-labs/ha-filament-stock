"""Tests for the sensors, the stock manager and the actions."""

from __future__ import annotations

from typing import Any

from conftest import FakeDrive, FakeTransport
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from threemf import REFERENCE_PLATE, build_3mf, slice_info

from custom_components.filament_stock.bambu_api import FILAMENT_PATH, Response
from custom_components.filament_stock.const import DOMAIN
from custom_components.filament_stock.models import GroupKey

BLACK = {
    "id": 1,
    "color": "#000000FF",
    "filamentType": "PLA",
    "netWeight": 1000,
    "totalNetWeight": 1000,
    "filamentName": "PLA Basic",
}
GREY = {
    "id": 2,
    "color": "#898989FF",
    "filamentType": "PLA",
    "netWeight": 800,
    "totalNetWeight": 1000,
}
GREEN_MAT = {
    "id": 3,
    "color": "#006633FF",
    "filamentType": "PLA",
    "note": "mat",
    "netWeight": 500,
    "totalNetWeight": 1000,
}
GREEN_GLOSS = {
    "id": 4,
    "color": "#006633FF",
    "filamentType": "PLA",
    "note": "hoogglans",
    "netWeight": 300,
    "totalNetWeight": 1000,
}

UNKNOWN = "unknown_petg_ff0000"


def _spools(bambu: FakeTransport, *hits: dict[str, Any]) -> None:
    bambu.routes.pop(("GET", FILAMENT_PATH), None)
    bambu.json("GET", FILAMENT_PATH, {"total": len(hits), "hits": list(hits)})


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


def _entity(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> str | None:
    domain = "number" if key.endswith("_stock") and key != "total_stock" else "sensor"
    return er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{entry.entry_id}_{key}"
    )


def _value(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> float:
    entity_id = _entity(hass, entry, key)
    assert entity_id, key
    return float(hass.states.get(entity_id).state)


@pytest.fixture
def plate(drive: FakeDrive) -> FakeDrive:
    drive.add("f1", "plate.gcode.3mf", build_3mf(slice_info([REFERENCE_PLATE])))
    return drive


async def test_reference_plate_balance(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, plate: FakeDrive
) -> None:
    _spools(bambu, BLACK, GREY)
    await _setup(hass, entry)

    # #101410 has no exact spool: nearest is black. #898989 is exact.
    assert _value(hass, entry, "pla_000000_stock") == 1000
    assert _value(hass, entry, "pla_000000_needed") == pytest.approx(15.0)
    assert _value(hass, entry, "pla_000000_balance") == pytest.approx(985.0)
    assert _value(hass, entry, "pla_898989_balance") == pytest.approx(788.9)
    assert _value(hass, entry, "total_needed") == pytest.approx(26.1)
    assert _value(hass, entry, "total_stock") == 1800
    assert _value(hass, entry, "queue_plates") == 1
    assert _value(hass, entry, "unmatched_colors") == 0

    state = hass.states.get(_entity(hass, entry, "pla_000000_balance"))
    assert state.attributes["material"] == "PLA"
    assert state.attributes["color"] == "#000000"
    assert (state.attributes["r"], state.attributes["g"], state.attributes["b"]) == (
        0,
        0,
        0,
    )
    grey = hass.states.get(_entity(hass, entry, "pla_898989_stock"))
    assert (grey.attributes["r"], grey.attributes["g"], grey.attributes["b"]) == (
        137,
        137,
        137,
    )
    assert state.attributes["spools"][0]["name"] == "PLA Basic"
    assert state.attributes["file_colors"][0]["method"] == "nearest"
    assert state.attributes["unit_of_measurement"] == "g"
    assert state.name == "Filament Stock PLA #000000 balance"


async def test_notes_make_separate_groups(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, GREEN_MAT, GREEN_GLOSS)
    await _setup(hass, entry)
    mat = GroupKey.of("PLA", "#006633", "mat").id
    gloss = GroupKey.of("PLA", "#006633", "hoogglans").id
    assert _value(hass, entry, f"{mat}_balance") == 500
    assert _value(hass, entry, f"{gloss}_balance") == 300
    state = hass.states.get(_entity(hass, entry, f"{mat}_balance"))
    assert state.attributes["note"] == "mat"
    assert state.name == "Filament Stock PLA #006633 mat balance"


async def test_unknown_color_goes_negative(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    drive.add(
        "f1", "red.gcode.3mf", build_3mf(slice_info([[("PETG", "#FF0000", 7.5)]]))
    )
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    assert _value(hass, entry, f"{UNKNOWN}_balance") == pytest.approx(-7.5)
    assert _value(hass, entry, "unmatched_colors") == 1
    state = hass.states.get(_entity(hass, entry, "unmatched_colors"))
    assert state.attributes["colors"] == [{"material": "PETG", "color": "#FF0000"}]
    unknown = hass.states.get(_entity(hass, entry, f"{UNKNOWN}_balance"))
    assert unknown.attributes["unknown"] is True


async def test_vanished_group_is_removed(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK, GREY)
    await _setup(hass, entry)
    assert _entity(hass, entry, "pla_898989_stock")

    _spools(bambu, BLACK)
    await entry.runtime_data.spools.async_refresh()
    await hass.async_block_till_done()
    assert _entity(hass, entry, "pla_898989_stock") is None
    assert _entity(hass, entry, "pla_898989_balance") is None
    assert _entity(hass, entry, "pla_000000_stock")


async def test_new_group_appears(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    _spools(bambu, BLACK, GREY)
    await entry.runtime_data.spools.async_refresh()
    await hass.async_block_till_done()
    assert _value(hass, entry, "pla_898989_stock") == 800
    assert entry.runtime_data.manager.last_sync.new == [2]


async def test_failed_refresh_keeps_values(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    bambu.routes[("GET", FILAMENT_PATH)] = [Response(503, "down")]
    await entry.runtime_data.spools.async_refresh()
    await hass.async_block_till_done()
    assert _value(hass, entry, "pla_000000_stock") == 1000


async def test_nearest_can_be_disabled(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, plate: FakeDrive
) -> None:
    hass.config_entries.async_update_entry(entry, options={"match_nearest": False})
    _spools(bambu, BLACK, GREY)
    await _setup(hass, entry)
    assert _value(hass, entry, "unmatched_colors") == 1
    assert _value(hass, entry, "unknown_pla_101410_balance") == pytest.approx(-15.0)
    assert _value(hass, entry, "pla_000000_needed") == 0


async def test_map_color_action(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    plate: FakeDrive,
    hass_storage: dict[str, Any],
) -> None:
    _spools(bambu, BLACK, GREY)
    await _setup(hass, entry)
    grey_balance = _entity(hass, entry, "pla_898989_balance")
    await hass.services.async_call(
        DOMAIN,
        "map_color",
        {"material": "PLA", "color": "#101410", "group": grey_balance},
        blocking=True,
    )
    assert _value(hass, entry, "pla_000000_needed") == 0
    assert _value(hass, entry, "pla_898989_needed") == pytest.approx(26.1)

    # Without a group the mapping is removed.
    await hass.services.async_call(
        DOMAIN, "map_color", {"material": "PLA", "color": "#101410"}, blocking=True
    )
    assert _value(hass, entry, "pla_000000_needed") == pytest.approx(15.0)


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"material": "PLA", "color": "black"}, "invalid_color"),
        (
            {"material": "PLA", "color": "#101410", "group": "sensor.nope"},
            "not_a_group",
        ),
    ],
)
async def test_map_color_errors(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    data: dict[str, str],
    key: str,
) -> None:
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(DOMAIN, "map_color", data, blocking=True)
    assert info.value.translation_key == key


async def test_map_color_to_unknown_group_is_refused(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    drive.add("f1", "red.gcode.3mf", build_3mf(slice_info([[("PETG", "#FF0000", 1)]])))
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            DOMAIN,
            "map_color",
            {
                "material": "PLA",
                "color": "#101410",
                "group": _entity(hass, entry, f"{UNKNOWN}_balance"),
            },
            blocking=True,
        )
    assert info.value.translation_key == "unknown_group_target"


async def test_reset_ledger_action(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    manager = entry.runtime_data.manager
    manager.ledger.deduct("job-1", {"pla_000000": 1100}, manager.spools.data.groups)
    manager.async_update()
    assert _value(hass, entry, "pla_000000_stock") == -100

    stock = _entity(hass, entry, "pla_000000_stock")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "reset_ledger", {"group": stock, "spool_id": 99}, blocking=True
        )
    await hass.services.async_call(
        DOMAIN, "reset_ledger", {"group": stock, "spool_id": 1}, blocking=True
    )
    assert _value(hass, entry, "pla_000000_stock") == 900  # shortage stays
    await hass.services.async_call(
        DOMAIN, "reset_ledger", {"group": stock}, blocking=True
    )
    assert _value(hass, entry, "pla_000000_stock") == 1000


async def test_refresh_action(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    await _setup(hass, entry)
    before = len(bambu.calls)
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)
    assert len(bambu.calls) == before + 1
    await hass.services.async_call(
        DOMAIN, "refresh", {"config_entry_id": entry.entry_id}, blocking=True
    )
    assert len(bambu.calls) == before + 2
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "refresh", {"config_entry_id": "nope"}, blocking=True
        )


async def test_ledger_survives_restart(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    manager = entry.runtime_data.manager
    manager.ledger.deduct("job-1", {"pla_000000": 100}, manager.spools.data.groups)
    manager.async_save()
    manager.async_update()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    await _setup(hass, entry)
    assert _value(hass, entry, "pla_000000_stock") == 900
    assert "job-1" in entry.runtime_data.manager.ledger.applied


def _stale_entity(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    return (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_pla_123456_balance",
            config_entry=entry,
            translation_key="balance",
        )
        .entity_id
    )


async def test_group_gone_while_offline_is_removed(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    stale = _stale_entity(hass, entry)
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    assert er.async_get(hass).async_get(stale) is None
    assert _entity(hass, entry, "pla_000000_balance")


async def test_no_cleanup_without_real_data(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    stale = _stale_entity(hass, entry)
    bambu.routes[("GET", FILAMENT_PATH)] = [Response(503, "down")]
    await _setup(hass, entry)
    assert er.async_get(hass).async_get(stale) is not None


async def test_shortage_survives_start_without_spool_cache(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    hass_storage: dict[str, Any],
) -> None:
    hass_storage[f"{DOMAIN}.{entry.entry_id}.stock"] = {
        "version": 1,
        "key": f"{DOMAIN}.{entry.entry_id}.stock",
        "data": {
            "ledger": {
                "spools": {"1": [1000, 0]},
                "shortage": {"pla_000000": 25.0},
                "applied": ["job-1"],
                "synced": True,
            }
        },
    }
    bambu.routes[("GET", FILAMENT_PATH)] = [Response(503, "down")]
    await _setup(hass, entry)
    assert entry.runtime_data.manager.ledger.shortage == {"pla_000000": 25.0}


async def test_empty_cloud_list_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    manager = entry.runtime_data.manager
    manager.ledger.deduct("job-1", {"pla_000000": 100}, manager.spools.data.groups)

    _spools(bambu)
    await entry.runtime_data.spools.async_refresh()
    await hass.async_block_till_done()
    assert not entry.runtime_data.spools.last_update_success
    assert manager.ledger.remaining(1) == 900
    assert _value(hass, entry, "pla_000000_stock") == 900


async def test_stock_can_be_set(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    stock = _entity(hass, entry, "pla_000000_stock")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": stock, "value": 1200}, blocking=True
    )
    assert _value(hass, entry, "pla_000000_stock") == 1200
    assert _value(hass, entry, "pla_000000_balance") == 1200
    assert hass.states.get(stock).attributes["correction"] == 200

    # A print deducts from the corrected stock.
    manager = entry.runtime_data.manager
    manager.ledger.deduct("job-1", {"pla_000000": 100}, manager.spools.data.groups)
    manager.async_update()
    assert _value(hass, entry, "pla_000000_stock") == 1100

    # A new weight in Bambu Studio wins over the local value.
    _spools(bambu, {**BLACK, "netWeight": 700})
    await entry.runtime_data.spools.async_refresh()
    await hass.async_block_till_done()
    assert _value(hass, entry, "pla_000000_stock") == 700


async def test_old_stock_sensors_are_removed(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, drive: FakeDrive
) -> None:
    old = (
        er.async_get(hass)
        .async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_pla_000000_stock",
            config_entry=entry,
            translation_key="stock",
        )
        .entity_id
    )
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    assert er.async_get(hass).async_get(old) is None
    assert _value(hass, entry, "pla_000000_stock") == 1000


async def test_file_color_select(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, plate: FakeDrive
) -> None:
    _spools(bambu, BLACK, GREY)
    await _setup(hass, entry)
    select = er.async_get(hass).async_get_entity_id(
        "select", DOMAIN, f"{entry.entry_id}_pla_101410_file_color"
    )
    state = hass.states.get(select)
    assert state.state == "auto"
    assert state.attributes["options"] == ["auto", "PLA #000000", "PLA #898989"]
    assert state.attributes["counted_as"] == "PLA #000000"
    assert state.attributes["method"] == "nearest"
    assert state.attributes["files"] == ["plate.gcode.3mf"]
    assert state.name == "Filament Stock PLA #101410 in files"

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "PLA #898989"},
        blocking=True,
    )
    assert hass.states.get(select).state == "PLA #898989"
    assert _value(hass, entry, "pla_898989_needed") == pytest.approx(26.1)
    assert _value(hass, entry, "pla_000000_needed") == 0

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": select, "option": "auto"},
        blocking=True,
    )
    assert _value(hass, entry, "pla_000000_needed") == pytest.approx(15.0)


async def test_every_needed_color_gets_a_queue_select(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport, plate: FakeDrive
) -> None:
    """A color with grams in the queue always has a live select, also when added later.

    The queue color list on the dashboard is built from these selects.
    """
    brown = {
        "id": 9,
        "color": "#996633FF",
        "filamentType": "PLA",
        "note": "DFP14514 Wood-Brown mat",
        "netWeight": 1000,
        "totalNetWeight": 1000,
    }
    _spools(bambu, BLACK, GREY, brown)
    await _setup(hass, entry)
    plate.add(
        "f2",
        "brown.gcode.3mf",
        build_3mf(slice_info([[("PLA", "#996633", 226.08)]])),
    )
    await entry.runtime_data.queue.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    manager = entry.runtime_data.manager
    brown_id = GroupKey.of("PLA", "#996633", "DFP14514 Wood-Brown mat").id
    assert _value(hass, entry, f"{brown_id}_needed") == pytest.approx(226.1)
    registry = er.async_get(hass)
    for material, color in manager.needs.matches:
        key = GroupKey.of(material, color, "").id
        select = registry.async_get_entity_id(
            "select", DOMAIN, f"{entry.entry_id}_{key}_file_color"
        )
        assert select, color
        state = hass.states.get(select)
        assert state is not None
        assert state.state != "unavailable", color
    state = hass.states.get(
        registry.async_get_entity_id(
            "select", DOMAIN, f"{entry.entry_id}_pla_996633_file_color"
        )
    )
    assert state.attributes["method"] == "exact"
    assert state.attributes["files"] == ["brown.gcode.3mf"]
