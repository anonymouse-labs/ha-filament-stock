"""Tests for notifications and repair issues."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import time
from typing import Any
from unittest.mock import MagicMock, patch

from conftest import FakeDrive, FakeTransport
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)
from threemf import build_3mf, slice_info

from custom_components.filament_stock.bambu_api import FILAMENT_PATH
from custom_components.filament_stock.const import CONF_TOKEN_EXPIRES, DOMAIN
from custom_components.filament_stock.notifier import RATE_LIMIT

BLACK = {
    "id": 1,
    "color": "#000000FF",
    "filamentType": "PLA",
    "netWeight": 100,
    "totalNetWeight": 1000,
    "filamentName": "PLA Basic",
}
GREY = {"id": 2, "color": "#898989FF", "filamentType": "PLA", "netWeight": 1000,
        "totalNetWeight": 1000, "filamentName": "PLA Matte"}  # fmt: skip


@pytest.fixture
def sent() -> Generator[MagicMock]:
    with patch(
        "custom_components.filament_stock.notifier.persistent_notification.async_create"
    ) as create:
        yield create


def _spools(bambu: FakeTransport, *hits: dict[str, Any]) -> None:
    bambu.routes.pop(("GET", FILAMENT_PATH), None)
    bambu.json("GET", FILAMENT_PATH, {"total": len(hits), "hits": list(hits)})


def _plate(drive: FakeDrive, grams: float, file_id: str = "f1") -> None:
    drive.listing = [f for f in drive.listing if f.id != file_id]
    drive.add(
        file_id,
        f"{file_id}.gcode.3mf",
        build_3mf(slice_info([[("PLA", "#000000", grams)]])),
        stamp=str(grams),
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _wait(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.spools.async_refresh()
    await entry.runtime_data.queue.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


def _messages(sent: MagicMock) -> list[str]:
    return [c.args[1] for c in sent.call_args_list]


async def test_negative_balance_once_then_rearm(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1
    message = _messages(sent)[0]
    assert "PLA #000000" in message
    assert "-50" in message
    assert sent.call_args.kwargs["title"] == "Filament Stock"

    # Still negative: nothing new.
    await _refresh(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1

    # Recovers, then drops again: one new message.
    _plate(drive, 50)
    await _refresh(hass, entry)
    _plate(drive, 200)
    await _refresh(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 2


async def test_alert_survives_restart_without_repeat(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _setup(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1


async def test_new_spool_and_low_balance(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    hass.config_entries.async_update_entry(entry, options={"notify_low": 200})
    _spools(bambu, BLACK)
    await _setup(hass, entry)
    await _wait(hass)
    # The first sync never reports existing spools as new. Black is low.
    assert len(sent.call_args_list) == 1
    assert "below 200 g" in _messages(sent)[0]

    _spools(bambu, BLACK, GREY)
    await _refresh(hass, entry)
    await _wait(hass)
    assert "New spool: PLA #898989 (PLA Matte), 1000 g" in _messages(sent)[1]


async def test_kinds_can_be_switched_off(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    hass.config_entries.async_update_entry(
        entry,
        options={"notify_negative": False, "notify_not_sliced": False},
    )
    _spools(bambu, BLACK)
    _plate(drive, 150)
    drive.add("f2", "project.3mf", build_3mf(None))
    await _setup(hass, entry)
    await _wait(hass)
    sent.assert_not_called()


async def test_not_sliced_and_token(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TOKEN_EXPIRES: int(time.time() + 5 * 86400)}
    )
    drive.add("f2", "project.3mf", build_3mf(None))
    await _setup(hass, entry)
    await _wait(hass)
    message = _messages(sent)[0]
    assert "Not sliced: project.3mf" in message
    assert "expires in 4 days" in message or "expires in 5 days" in message

    await _refresh(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1


async def test_notify_entity(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    calls = async_mock_service(hass, "notify", "send_message")
    hass.config_entries.async_update_entry(
        entry,
        options={"notify_method": "notify_entity", "notify_entity": "notify.phone"},
    )
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert len(calls) == 1
    assert calls[0].data["entity_id"] == "notify.phone"
    assert calls[0].data["title"] == "Filament Stock"
    sent.assert_not_called()


async def test_whatsapp_notifier(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    calls = async_mock_service(hass, "whatsapp_notifier", "send_message")
    hass.config_entries.async_update_entry(
        entry, options={"notify_method": "whatsapp", "notify_chat_id": "group-1"}
    )
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert len(calls) == 1
    assert calls[0].data["chat_id"] == "group-1"
    assert calls[0].data["message"].startswith("*Filament Stock*\n")


async def test_failed_send_falls_back(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    # whatsapp_notifier is not loaded: the call fails.
    hass.config_entries.async_update_entry(entry, options={"notify_method": "whatsapp"})
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1


async def test_rate_limit(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    await _setup(hass, entry)
    notifier = entry.runtime_data.notifier
    for i in range(RATE_LIMIT + 5):
        notifier.pending.append(f"line {i}")
        await notifier.async_send_pending()
    assert len(sent.call_args_list) == RATE_LIMIT
    assert notifier.pending == ["line 20", "line 21", "line 22", "line 23", "line 24"]


async def test_dutch_messages(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    hass.config.language = "nl"
    _spools(bambu, BLACK)
    _plate(drive, 150)
    await _setup(hass, entry)
    await _wait(hass)
    assert "Te weinig filament: PLA #000000 saldo -50 g" in _messages(sent)[0]


async def test_repair_issues(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    drive.add("f2", "project.3mf", build_3mf(None))
    drive.add("f3", "red.gcode.3mf", build_3mf(slice_info([[("PETG", "#FF0000", 4)]])))
    await _setup(hass, entry)
    registry = ir.async_get(hass)
    sliced = registry.async_get_issue(DOMAIN, f"not_sliced_{entry.entry_id}")
    assert sliced.translation_placeholders == {"files": "project.3mf"}
    unmatched = registry.async_get_issue(DOMAIN, f"unmatched_{entry.entry_id}")
    assert unmatched.translation_placeholders == {"colors": "PETG #FF0000"}

    drive.listing.clear()
    await _refresh(hass, entry)
    assert registry.async_get_issue(DOMAIN, f"not_sliced_{entry.entry_id}") is None
    assert registry.async_get_issue(DOMAIN, f"unmatched_{entry.entry_id}") is None


async def test_unknown_color_suggests_nearest_or_new_spool(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    drive: FakeDrive,
    sent: MagicMock,
) -> None:
    hass.config_entries.async_update_entry(entry, options={"notify_negative": False})
    _spools(bambu, GREY)
    drive.add("f2", "brick.gcode.3mf", build_3mf(slice_info([[("PLA", "#C12E1F", 4)]])))
    drive.add("f3", "flex.gcode.3mf", build_3mf(slice_info([[("TPU", "#00FF00", 2)]])))
    await _setup(hass, entry)
    await _wait(hass)
    message = _messages(sent)[0]
    assert "Unknown color PLA #C12E1F in brick.gcode.3mf" in message
    assert "Nearest in stock: PLA #898989" in message
    assert "There is no TPU in stock" in message
    assert "material TPU, color #00FF00" in message

    await _refresh(hass, entry)
    await _wait(hass)
    assert len(sent.call_args_list) == 1
