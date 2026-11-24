"""Tests for the Google Drive client."""

from __future__ import annotations

import json
from unittest.mock import patch

from aiohttp import ClientError
from conftest import FOLDER_ID, SA_EMAIL
from google.auth.exceptions import RefreshError, TransportError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.filament_stock.drive import (
    DRIVE_API,
    DriveApiDisabled,
    DriveClient,
    DriveError,
    DriveFileTooLarge,
    DriveForbidden,
    DriveInvalidFolderId,
    DriveInvalidJson,
    DriveInvalidServiceAccount,
    DriveNotAFolder,
    DriveNotFound,
    DriveRateLimited,
    parse_folder_id,
    parse_service_account,
)

FOLDER_URL = f"{DRIVE_API}/files/{FOLDER_ID}"
FILES_URL = f"{DRIVE_API}/files"
FILE_ID = "fileId0123456789"


def _error(reason: str) -> dict:
    return {"error": {"code": 403, "errors": [{"reason": reason}]}}


@pytest.fixture
async def client(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service_account_json: str,
    mock_token: None,
    no_backoff: None,
) -> DriveClient:
    return DriveClient(async_get_clientsession(hass), service_account_json)


@pytest.mark.parametrize(
    "value",
    [
        FOLDER_ID,
        f"  {FOLDER_ID} ",
        f"https://drive.google.com/drive/folders/{FOLDER_ID}",
        f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}?usp=sharing",
    ],
)
def test_parse_folder_id(value: str) -> None:
    assert parse_folder_id(value) == FOLDER_ID


@pytest.mark.parametrize("value", ["", "short", "abc' or 'x' in parents", "a/b/c"])
def test_parse_folder_id_rejects(value: str) -> None:
    with pytest.raises(DriveInvalidFolderId):
        parse_folder_id(value)


def test_parse_service_account(service_account_json: str) -> None:
    assert parse_service_account(service_account_json)["client_email"] == SA_EMAIL
    with pytest.raises(DriveInvalidJson):
        parse_service_account('{"type": "service_account"')
    with pytest.raises(DriveInvalidJson):
        parse_service_account("[]")
    with pytest.raises(DriveInvalidServiceAccount):
        parse_service_account('{"type": "authorized_user"}')
    info = json.loads(service_account_json)
    info["token_uri"] = "https://attacker.example/token"
    with pytest.raises(DriveInvalidServiceAccount):
        parse_service_account(json.dumps(info))


async def test_bad_private_key(hass: HomeAssistant, service_account_json: str) -> None:
    info = json.loads(service_account_json)
    info["private_key"] = "not a key"
    with pytest.raises(DriveInvalidServiceAccount):
        DriveClient(async_get_clientsession(hass), json.dumps(info))


async def test_get_folder(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        FOLDER_URL,
        json={
            "id": FOLDER_ID,
            "name": "Queue",
            "mimeType": "application/vnd.google-apps.folder",
        },
    )
    assert await client.get_folder(FOLDER_ID) == "Queue"
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (404, {"error": {"code": 404}}, DriveNotFound),
        (403, _error("accessNotConfigured"), DriveApiDisabled),
        (
            403,
            {"error": {"details": [{"reason": "SERVICE_DISABLED"}]}},
            DriveApiDisabled,
        ),
        (403, _error("insufficientFilePermissions"), DriveForbidden),
        (403, _error("rateLimitExceeded"), DriveRateLimited),
        (429, {}, DriveRateLimited),
        (401, {}, DriveInvalidServiceAccount),
        (500, None, DriveError),
    ],
)
async def test_get_folder_errors(
    client: DriveClient,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    body: dict | None,
    error: type[DriveError],
) -> None:
    aioclient_mock.get(FOLDER_URL, status=status, json=body)
    with pytest.raises(error):
        await client.get_folder(FOLDER_ID)


async def test_get_folder_on_a_file(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(FOLDER_URL, json={"id": FOLDER_ID, "mimeType": "text/plain"})
    with pytest.raises(DriveNotAFolder):
        await client.get_folder(FOLDER_ID)


async def test_network_error(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(FOLDER_URL, exc=ClientError())
    with pytest.raises(DriveError) as info:
        await client.get_folder(FOLDER_ID)
    assert info.value.key == "cannot_connect"
    assert aioclient_mock.call_count == 3


async def test_retry_then_success(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    calls = 0

    async def flaky(method, url, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AiohttpClientMockResponse(method, url, status=429, json={})
        return AiohttpClientMockResponse(
            method,
            url,
            json={"name": "Queue", "mimeType": "application/vnd.google-apps.folder"},
        )

    aioclient_mock.get(FOLDER_URL, side_effect=flaky)
    assert await client.get_folder(FOLDER_ID) == "Queue"
    assert calls == 2


async def test_unexpected_host_is_refused(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    async def redirected(method, url, data):
        return AiohttpClientMockResponse(method, URL("https://evil.example/x"), json={})

    aioclient_mock.get(FOLDER_URL, side_effect=redirected)
    with pytest.raises(DriveError, match="unexpected host"):
        await client.get_folder(FOLDER_ID)


async def test_list_folder_pages(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    async def pages(method, url, data):
        if url.query.get("pageToken") == "p2":
            body = {"files": [{"id": "b", "name": "b.gcode.3mf", "modifiedTime": "t"}]}
        else:
            body = {
                "nextPageToken": "p2",
                "files": [
                    {
                        "id": "a",
                        "name": "a.gcode.3mf",
                        "md5Checksum": "m",
                        "size": "12",
                    },
                    {"name": "missing id"},
                ],
            }
        return AiohttpClientMockResponse(method, url, json=body)

    aioclient_mock.get(FILES_URL, side_effect=pages)
    files = await client.list_folder(FOLDER_ID)
    assert [(f.id, f.stamp, f.size) for f in files] == [
        ("a", "m", 12),
        ("b", "t", None),
    ]
    query = aioclient_mock.mock_calls[0][1].query["q"]
    assert f"'{FOLDER_ID}' in parents" in query
    assert "trashed=false" in query


async def test_list_empty_folder(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(FILES_URL, json={"files": []})
    assert await client.list_folder(FOLDER_ID) == []


async def test_download(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{FILES_URL}/{FILE_ID}", content=b"x" * 1000)
    assert await client.download(FILE_ID) == b"x" * 1000


async def test_download_declared_too_large(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{FILES_URL}/{FILE_ID}", content=b"x", headers={"Content-Length": "999999"}
    )
    with pytest.raises(DriveFileTooLarge):
        await client.download(FILE_ID, limit=100)


async def test_download_streamed_too_large(
    client: DriveClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{FILES_URL}/{FILE_ID}", content=b"x" * 1000)
    with pytest.raises(DriveFileTooLarge):
        await client.download(FILE_ID, limit=100)


async def test_download_rejects_bad_id(client: DriveClient) -> None:
    with pytest.raises(DriveInvalidFolderId):
        await client.download("../../etc")


@pytest.mark.parametrize(
    ("raised", "key"),
    [
        (RefreshError("bad"), "invalid_service_account"),
        (TransportError("x"), "cannot_connect"),
    ],
)
async def test_token_refresh_errors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    service_account_json: str,
    raised: Exception,
    key: str,
) -> None:
    client = DriveClient(async_get_clientsession(hass), service_account_json)
    with (
        patch.object(DriveClient, "_refresh", side_effect=raised),
        pytest.raises(DriveError) as info,
    ):
        await client.get_folder(FOLDER_ID)
    assert info.value.key == key
