"""Tests for the config, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from conftest import ACCOUNT, FOLDER_ID, SA_EMAIL, FakeTransport, make_jwt
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.filament_stock.bambu_api import (
    CSRF_PATH,
    LOGIN_PATH,
    TFA_PATH,
    Response,
)
from custom_components.filament_stock.const import (
    CONF_ACCOUNT,
    CONF_FOLDER_ID,
    CONF_QUEUE_INTERVAL,
    CONF_SERVICE_ACCOUNT,
    CONF_SPOOL_INTERVAL,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES,
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
NEW_TOKEN = make_jwt(1900000000)
USER_TOKEN = make_jwt(1900000000, username="u_42")
OTHER_FOLDER = "9ZyXwVuTsRqPoNmLkJiHgFeDcBa98765"


@pytest.fixture(autouse=True)
def no_setup():
    with (
        patch(
            "custom_components.filament_stock.async_setup_entry", return_value=True
        ) as setup,
        patch("custom_components.filament_stock.async_unload_entry", return_value=True),
    ):
        yield setup


async def _account_step(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"
    return result


async def _login(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ACCOUNT: f" {ACCOUNT} ", CONF_PASSWORD: "secret"}
    )


async def _drive(hass: HomeAssistant, flow_id: str, sa: str, folder: str = FOLDER_ID):
    with patch(GET_FOLDER, return_value="Queue"):
        return await hass.config_entries.flow.async_configure(
            flow_id, {CONF_SERVICE_ACCOUNT: sa, CONF_FOLDER_ID: folder}
        )


async def test_full_flow(
    hass: HomeAssistant, bambu: FakeTransport, service_account_json: str
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await _login(hass, (await _account_step(hass))["flow_id"])
    assert result["step_id"] == "drive"
    result = await _drive(
        hass,
        result["flow_id"],
        service_account_json,
        f"https://drive.google.com/drive/folders/{FOLDER_ID}",
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_ACCOUNT: ACCOUNT,
        CONF_TOKEN: NEW_TOKEN,
        CONF_TOKEN_EXPIRES: 1900000000,
        CONF_SERVICE_ACCOUNT: service_account_json,
        CONF_FOLDER_ID: FOLDER_ID,
    }
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == ACCOUNT
    assert bambu.closed


async def test_email_code_flow(
    hass: HomeAssistant, bambu: FakeTransport, service_account_json: str
) -> None:
    bambu.json("POST", LOGIN_PATH, {"loginType": "verifyCode"})
    bambu.add("POST", LOGIN_PATH, Response(400, '{"error": "bad code"}'))
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await _login(hass, (await _account_step(hass))["flow_id"])
    assert result["step_id"] == "verify_code"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "000000"}
    )
    assert result["errors"] == {"base": "invalid_code"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "123456"}
    )
    assert result["step_id"] == "drive"
    assert bambu.calls[-1]["json"] == {"account": ACCOUNT, "code": "123456"}


async def test_tfa_flow(
    hass: HomeAssistant, bambu: FakeTransport, service_account_json: str
) -> None:
    bambu.json("POST", LOGIN_PATH, {"loginType": "tfa", "tfaKey": "key-1"})
    bambu.add("GET", CSRF_PATH, Response(200, "", {"bbl_csrf_token": "c"}))
    bambu.add("POST", TFA_PATH, Response(200, "{}", {"token": NEW_TOKEN}))
    result = await _login(hass, (await _account_step(hass))["flow_id"])
    assert result["step_id"] == "tfa"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"code": "654321"}
    )
    assert result["step_id"] == "drive"
    result = await _drive(hass, result["flow_id"], service_account_json)
    assert result["data"][CONF_TOKEN] == NEW_TOKEN


@pytest.mark.parametrize(
    ("response", "key"),
    [
        (Response(400, '{"error": "Incorrect password"}'), "invalid_auth"),
        (Response(403, "cloudflare"), "cloudflare_blocked"),
        (Response(429, ""), "rate_limited"),
        (Response(503, ""), "cannot_connect"),
    ],
)
async def test_login_errors_recover(
    hass: HomeAssistant, bambu: FakeTransport, response: Response, key: str
) -> None:
    bambu.add("POST", LOGIN_PATH, response)
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    flow_id = (await _account_step(hass))["flow_id"]
    result = await _login(hass, flow_id)
    assert result["errors"] == {"base": key}
    result = await _login(hass, flow_id)
    assert result["step_id"] == "drive"


async def test_already_configured(
    hass: HomeAssistant, bambu: FakeTransport, entry: MockConfigEntry
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await _login(hass, (await _account_step(hass))["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


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
    bambu: FakeTransport,
    service_account_json: str,
    raised: Exception,
    field: str,
    key: str,
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await _login(hass, (await _account_step(hass))["flow_id"])
    with patch(GET_FOLDER, side_effect=raised):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_SERVICE_ACCOUNT: service_account_json, CONF_FOLDER_ID: FOLDER_ID},
        )
    assert result["errors"] == {field: key}
    assert result["description_placeholders"]["email"] == SA_EMAIL
    result = await _drive(hass, result["flow_id"], service_account_json)
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
async def test_drive_input_errors(
    hass: HomeAssistant,
    bambu: FakeTransport,
    service_account_json: str,
    service_account: str | None,
    folder: str,
    errors: dict[str, str],
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await _login(hass, (await _account_step(hass))["flow_id"])
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


async def test_reauth(
    hass: HomeAssistant, bambu: FakeTransport, entry: MockConfigEntry
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "account"
    result = await _login(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_TOKEN] == NEW_TOKEN
    assert entry.data[CONF_TOKEN_EXPIRES] == 1900000000
    assert entry.data[CONF_FOLDER_ID] == FOLDER_ID


async def test_reauth_wrong_account(
    hass: HomeAssistant, bambu: FakeTransport, entry: MockConfigEntry
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCOUNT: "other@example.com", CONF_PASSWORD: "x"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_TOKEN] != NEW_TOKEN


async def test_reconfigure_account(
    hass: HomeAssistant, bambu: FakeTransport, entry: MockConfigEntry
) -> None:
    bambu.json("POST", LOGIN_PATH, {"accessToken": NEW_TOKEN})
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )
    result = await _login(hass, result["flow_id"])
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_TOKEN] == NEW_TOKEN


async def test_reconfigure_drive_keeps_key(
    hass: HomeAssistant,
    bambu: FakeTransport,
    entry: MockConfigEntry,
    service_account_json: str,
) -> None:
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "drive"}
    )
    assert result["step_id"] == "drive"
    with patch(GET_FOLDER, return_value="Other") as get_folder:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_FOLDER_ID: OTHER_FOLDER}
        )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_FOLDER_ID] == OTHER_FOLDER
    assert entry.data[CONF_SERVICE_ACCOUNT] == service_account_json
    get_folder.assert_awaited_once_with(OTHER_FOLDER)


async def test_options_flow_reloads(
    hass: HomeAssistant, entry: MockConfigEntry, no_setup
) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = no_setup.call_count
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SPOOL_INTERVAL: 20,
            CONF_QUEUE_INTERVAL: 30,
            "match_nearest": False,
            "match_distance": 5,
        },
    )
    assert result["step_id"] == "notify"
    assert (
        "whatsapp"
        not in result["data_schema"].schema["notify_method"].config["options"]
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_method": "notify_entity",
            "notify_new_spool": True,
            "notify_negative": True,
            "notify_low": 100,
            "notify_not_sliced": False,
            "notify_token": True,
        },
    )
    assert result["errors"] == {"notify_entity": "notify_entity_required"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "notify_method": "notify_entity",
            "notify_entity": "notify.phone",
            "notify_new_spool": True,
            "notify_negative": True,
            "notify_low": 100,
            "notify_not_sliced": False,
            "notify_token": True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        CONF_SPOOL_INTERVAL: 20,
        CONF_QUEUE_INTERVAL: 30,
        "match_nearest": False,
        "match_distance": 5.0,
        "notify_method": "notify_entity",
        "notify_entity": "notify.phone",
        "notify_new_spool": True,
        "notify_negative": True,
        "notify_low": 100.0,
        "notify_not_sliced": False,
        "notify_token": True,
        "notify_unmatched": True,
    }
    assert no_setup.call_count == calls + 1


async def test_options_whatsapp_needs_integration(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.config.components.add("whatsapp_notifier")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SPOOL_INTERVAL: 15,
            CONF_QUEUE_INTERVAL: 10,
            "match_nearest": True,
            "match_distance": 10,
        },
    )
    assert "whatsapp" in result["data_schema"].schema["notify_method"].config["options"]
    assert "ha-whatsapp-notifier" in result["description_placeholders"]["whatsapp_url"]
