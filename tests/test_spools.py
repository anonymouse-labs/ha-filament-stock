"""Tests for the spool coordinator, token warning, setup and diagnostics."""

from __future__ import annotations

from datetime import timedelta
import time
from unittest.mock import patch

from conftest import ACCOUNT, TOKEN, FakeTransport
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.filament_stock.bambu_api import FILAMENT_PATH, Response
from custom_components.filament_stock.const import CONF_TOKEN_EXPIRES, DOMAIN
from custom_components.filament_stock.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.filament_stock.models import GroupKey
from custom_components.filament_stock.spools import token_issue_id

HITS = [
    {"id": 1, "color": "#006633FF", "filamentType": "PLA", "note": "mat",
     "netWeight": 1000, "totalNetWeight": 1000, "RFID": "AABBCC", "devId": "SERIAL1"},
    {"id": 2, "color": "#006633FF", "filamentType": "PLA", "note": "hoogglans",
     "netWeight": 400, "totalNetWeight": 1000},
    {"id": 3, "color": "#006633FF", "filamentType": "PLA", "note": "Mat",
     "netWeight": 250, "totalNetWeight": 1000},
]  # fmt: skip


@pytest.fixture(autouse=True)
def quiet_drive():
    """Drive is not under test here."""
    with (
        patch(
            "custom_components.filament_stock.drive.DriveClient.get_folder",
            return_value="Queue",
        ),
        patch(
            "custom_components.filament_stock.drive.DriveClient.list_folder",
            return_value=[],
        ),
    ):
        yield


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_spools_become_note_groups(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport
) -> None:
    bambu.json("GET", FILAMENT_PATH, {"total": 3, "hits": HITS})
    await _setup(hass, entry)
    data = entry.runtime_data.spools.data
    assert len(data.spools) == 3
    mat = data.groups[GroupKey.of("PLA", "#006633", "mat").id]
    glans = data.groups[GroupKey.of("PLA", "#006633", "hoogglans").id]
    assert [s.id for s in mat.spools] == [1, 3]
    assert [s.id for s in glans.spools] == [2]
    assert mat.label == "PLA #006633 mat"
    assert bambu.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


async def test_restart_uses_stored_list(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport
) -> None:
    bambu.json("GET", FILAMENT_PATH, {"total": 3, "hits": HITS})
    await _setup(hass, entry)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Cloud down on the next start: the stored list is used.
    bambu.routes.clear()
    bambu.add("GET", FILAMENT_PATH, Response(503, "down"))
    assert await hass.config_entries.async_setup(entry.entry_id)
    spools = entry.runtime_data.spools
    assert len(spools.data.spools) == 3
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not spools.last_update_success
    assert len(spools.data.spools) == 3


async def test_rejected_token_starts_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport
) -> None:
    bambu.add("GET", FILAMENT_PATH, Response(401, "{}"))
    await _setup(hass, entry)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_token_expiry_issue(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport
) -> None:
    bambu.json("GET", FILAMENT_PATH, {"total": 0, "hits": []})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TOKEN_EXPIRES: int(time.time() + 3 * 86400)}
    )
    await _setup(hass, entry)
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, token_issue_id(entry.entry_id))
    assert issue is not None
    assert issue.translation_placeholders in ({"days": "2"}, {"days": "3"})

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TOKEN_EXPIRES: int(time.time() + 60 * 86400)}
    )
    entry.runtime_data.spools.check_token_expiry()
    assert registry.async_get_issue(DOMAIN, token_issue_id(entry.entry_id)) is None


async def test_unload_closes_transport(
    hass: HomeAssistant, entry: MockConfigEntry, bambu: FakeTransport
) -> None:
    bambu.json("GET", FILAMENT_PATH, {"total": 0, "hits": []})
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert bambu.closed


async def test_diagnostics_redacts(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    bambu: FakeTransport,
    service_account_json: str,
) -> None:
    bambu.json("GET", FILAMENT_PATH, {"total": 3, "hits": HITS})
    await _setup(hass, entry)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    for secret in (ACCOUNT, TOKEN, "PRIVATE KEY", "AABBCC", "SERIAL1"):
        assert secret not in text
    assert diag["spools"]["count"] == 3
    assert diag["spools"]["raw"][0]["color"] == "#006633FF"
    assert diag["spools"]["raw"][0]["RFID"] == "**REDACTED**"
