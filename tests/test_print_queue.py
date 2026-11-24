"""Tests for the queue coordinator and setup."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from conftest import FOLDER_ID
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from threemf import REFERENCE_PLATE, build_3mf, slice_info

from custom_components.filament_stock.const import (
    CONF_FOLDER_ID,
    CONF_SERVICE_ACCOUNT,
    DOMAIN,
)
from custom_components.filament_stock.drive import (
    MAX_DOWNLOAD_BYTES,
    DriveError,
    DriveFile,
    DriveFileTooLarge,
    DriveNotFound,
)
from custom_components.filament_stock.parser import ParseStatus

SLICED = build_3mf(slice_info([REFERENCE_PLATE]))
PROJECT = build_3mf(None)
PLATE_NAME = "12 Gear BOX spare links v2.gcode.3mf"


class FakeDrive:
    """Stand-in for the Drive API calls."""

    def __init__(self) -> None:
        self.listing: list[DriveFile] = []
        self.content: dict[str, bytes | Exception] = {}
        self.folder_error: Exception | None = None
        self.downloads: list[str] = []
        self.gate: asyncio.Event | None = None

    async def get_folder(self, folder_id: str) -> str:
        if self.folder_error:
            raise self.folder_error
        return "Print queue"

    async def list_folder(self, folder_id: str) -> list[DriveFile]:
        return list(self.listing)

    async def download(self, file_id: str, limit: int = 0) -> bytes:
        self.downloads.append(file_id)
        if self.gate is not None:
            await self.gate.wait()
        value = self.content[file_id]
        if isinstance(value, Exception):
            raise value
        return value

    def add(self, file_id: str, name: str, data: bytes | Exception, stamp: str = "1"):
        self.listing.append(DriveFile(file_id, name, stamp, None))
        self.content[file_id] = data


@pytest.fixture
def drive() -> Generator[FakeDrive]:
    fake = FakeDrive()
    with (
        patch(
            "custom_components.filament_stock.drive.DriveClient.get_folder",
            side_effect=fake.get_folder,
        ),
        patch(
            "custom_components.filament_stock.drive.DriveClient.list_folder",
            side_effect=fake.list_folder,
        ),
        patch(
            "custom_components.filament_stock.drive.DriveClient.download",
            side_effect=fake.download,
        ),
    ):
        yield fake


@pytest.fixture
def entry(hass: HomeAssistant, service_account_json: str) -> MockConfigEntry:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=FOLDER_ID,
        data={CONF_SERVICE_ACCOUNT: service_account_json, CONF_FOLDER_ID: FOLDER_ID},
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data.queue


def _flush_store(hass: HomeAssistant) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))


async def test_reads_reference_plate(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", PLATE_NAME, SLICED)
    queue = await _setup(hass, entry)
    data = queue.data
    assert data.folder_name == "Print queue"
    assert data.plates == 1
    assert data.usage() == {
        ("PLA", "#101410"): pytest.approx(15.02),
        ("PLA", "#898989"): pytest.approx(11.12),
    }


async def test_sorts_files(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", "plate.gcode.3mf", SLICED)
    drive.add("f2", "project.3mf", PROJECT)
    drive.add("f3", "notes.txt", b"")
    drive.listing.append(DriveFile("f4", "huge.gcode.3mf", "1", MAX_DOWNLOAD_BYTES + 1))
    queue = await _setup(hass, entry)
    assert queue.data.not_sliced == ["huge.gcode.3mf", "project.3mf"]
    assert queue.data.files["f4"].parsed.status is ParseStatus.TOO_LARGE
    assert queue.data.other_files == ["notes.txt"]
    assert "f3" not in drive.downloads
    assert "f4" not in drive.downloads


async def test_unchanged_files_are_not_downloaded_again(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", "plate.gcode.3mf", SLICED)
    queue = await _setup(hass, entry)
    await queue.async_refresh()
    assert drive.downloads == ["f1"]

    drive.listing[0] = DriveFile("f1", "plate.gcode.3mf", "2", None)
    await queue.async_refresh()
    assert drive.downloads == ["f1", "f1"]


async def test_failed_download_keeps_previous_version(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", "plate.gcode.3mf", SLICED)
    queue = await _setup(hass, entry)

    drive.listing[0] = DriveFile("f1", "plate.gcode.3mf", "2", None)
    drive.content["f1"] = DriveError()
    await queue.async_refresh()
    item = queue.data.files["f1"]
    assert item.parsed.sliced
    assert item.error == "cannot_connect"


async def test_failed_new_download_is_pending(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", "plate.gcode.3mf", DriveError())
    drive.add("f2", "big.gcode.3mf", DriveFileTooLarge())
    queue = await _setup(hass, entry)
    assert queue.data.pending == ["plate.gcode.3mf"]
    assert queue.data.not_sliced == ["big.gcode.3mf"]
    assert queue.last_update_success


async def test_folder_error_fails_update(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.folder_error = DriveNotFound()
    queue = await _setup(hass, entry)
    assert not queue.last_update_success
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_does_not_wait_for_downloads(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", "plate.gcode.3mf", SLICED)
    drive.gate = asyncio.Event()
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    queue = entry.runtime_data.queue
    assert queue.data.files == {}
    drive.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert queue.data.plates == 1


async def test_restart_reads_cache_first(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", PLATE_NAME, SLICED)
    await _setup(hass, entry)
    _flush_store(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await hass.config_entries.async_unload(entry.entry_id)

    # Second start: Drive hangs, the cached result is available at once.
    drive.gate = asyncio.Event()
    drive.listing[0] = DriveFile("f1", PLATE_NAME, "changed", None)
    assert await hass.config_entries.async_setup(entry.entry_id)
    queue = entry.runtime_data.queue
    assert queue.data.plates == 1
    assert queue.find(["12 Gear BOX spare links v2"]) is not None
    drive.gate.set()
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_moved_file_is_still_found(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    drive.add("f1", PLATE_NAME, SLICED)
    queue = await _setup(hass, entry)
    drive.listing.clear()
    await queue.async_refresh()
    assert queue.data.plates == 0
    hit = queue.find(["/data/Metadata/12_Gear_BOX_spare_links_v2.gcode"])
    assert hit is not None
    assert hit[0] == PLATE_NAME
    assert hit[1].usage()[("PLA", "#898989")] == pytest.approx(11.12)
    assert queue.find([]) is None
    assert queue.find(["something else"]) is None


async def test_bad_service_account_fails_setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_SERVICE_ACCOUNT: "{}", CONF_FOLDER_ID: FOLDER_ID}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_options_change_reloads(
    hass: HomeAssistant, entry: MockConfigEntry, drive: FakeDrive
) -> None:
    await _setup(hass, entry)
    with patch(
        "custom_components.filament_stock.async_setup_entry",
        AsyncMock(return_value=True),
    ) as setup:
        hass.config_entries.async_update_entry(entry, options={"queue_interval": 5})
        await hass.async_block_till_done(wait_background_tasks=True)
    assert setup.called
