"""Tests for the config and options flow."""

from __future__ import annotations

from unittest.mock import patch

from conftest import FOLDER_ID, SA_EMAIL
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.filament_stock.const import (
    CONF_FOLDER_ID,
    CONF_QUEUE_INTERVAL,
    CONF_SERVICE_ACCOUNT,
    DOMAIN,
)
from custom_components.filament_stock.drive import (
    DriveApiDisabled,
    DriveError,
    DriveForbidden,
    DriveNotAFolder,
    DriveNotFound,
)

GET_FOLDER = "custom_components.filament_stock.config_flow.DriveClient.get_folder"


@pytest.fixture(autouse=True)
def no_setup():
    with patch("custom_components.filament_stock.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "drive"
    return result


async def test_success_with_folder_url(
    hass: HomeAssistant, service_account_json: str
) -> None:
    result = await _start(hass)
    with patch(GET_FOLDER, return_value="Queue") as get_folder:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SERVICE_ACCOUNT: service_account_json,
                CONF_FOLDER_ID: f"https://drive.google.com/drive/folders/{FOLDER_ID}",
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_SERVICE_ACCOUNT: service_account_json,
        CONF_FOLDER_ID: FOLDER_ID,
    }
    assert result["result"].unique_id == FOLDER_ID
    get_folder.assert_awaited_once_with(FOLDER_ID)


@pytest.mark.parametrize(
    ("raised", "field", "key"),
    [
        (DriveNotFound(), CONF_FOLDER_ID, "folder_not_found"),
        (DriveNotAFolder(), CONF_FOLDER_ID, "not_a_folder"),
        (DriveForbidden(), "base", "forbidden"),
        (DriveApiDisabled(), "base", "drive_api_disabled"),
        (DriveError(), "base", "cannot_connect"),
    ],
)
async def test_drive_errors(
    hass: HomeAssistant,
    service_account_json: str,
    raised: Exception,
    field: str,
    key: str,
) -> None:
    result = await _start(hass)
    with patch(GET_FOLDER, side_effect=raised):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SERVICE_ACCOUNT: service_account_json, CONF_FOLDER_ID: FOLDER_ID},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: key}
    assert result["description_placeholders"]["email"] == SA_EMAIL

    # Recover after fixing the problem.
    with patch(GET_FOLDER, return_value="Queue"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SERVICE_ACCOUNT: service_account_json, CONF_FOLDER_ID: FOLDER_ID},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("service_account", "folder", "errors"),
    [
        ('{"type": ', FOLDER_ID, {CONF_SERVICE_ACCOUNT: "invalid_json"}),
        (
            '{"type": "user"}',
            FOLDER_ID,
            {CONF_SERVICE_ACCOUNT: "invalid_service_account"},
        ),
        (None, "not/a folder", {CONF_FOLDER_ID: "invalid_folder_id"}),
    ],
)
async def test_input_errors(
    hass: HomeAssistant,
    service_account_json: str,
    service_account: str | None,
    folder: str,
    errors: dict[str, str],
) -> None:
    result = await _start(hass)
    with patch(GET_FOLDER) as get_folder:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_SERVICE_ACCOUNT: service_account or service_account_json,
                CONF_FOLDER_ID: folder,
            },
        )
    assert result["errors"] == errors
    get_folder.assert_not_called()


async def test_already_configured(
    hass: HomeAssistant, service_account_json: str
) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=FOLDER_ID).add_to_hass(hass)
    result = await _start(hass)
    with patch(GET_FOLDER, return_value="Queue"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SERVICE_ACCOUNT: service_account_json, CONF_FOLDER_ID: FOLDER_ID},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=FOLDER_ID)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_QUEUE_INTERVAL: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_QUEUE_INTERVAL: 30}
