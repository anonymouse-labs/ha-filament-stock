"""Tests for the Bambu Lab cloud client."""

from __future__ import annotations

import time
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from conftest import ACCOUNT, FakeTransport, make_jwt
from curl_cffi.requests import RequestsError
import pytest

from custom_components.filament_stock.bambu_api import (
    CSRF_PATH,
    FILAMENT_PATH,
    LOGIN_PATH,
    SEND_CODE_PATH,
    TFA_PATH,
    BambuAuthError,
    BambuBlocked,
    BambuCloudClient,
    BambuCodeError,
    BambuCodeExpired,
    BambuError,
    BambuRateLimited,
    CurlTransport,
    Response,
    parse_spool,
    token_expiry,
    token_user,
)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


async def test_login_returns_token(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"accessToken": "tok", "loginType": ""})
    client = BambuCloudClient(transport)
    result = await client.login(ACCOUNT, "secret")
    assert result.token == "tok"
    assert client.token == "tok"
    call = transport.calls[0]
    assert call["url"] == f"https://api.bambulab.com{LOGIN_PATH}"
    assert call["json"] == {"account": ACCOUNT, "password": "secret", "apiError": ""}
    assert "Authorization" not in call["headers"]


async def test_login_with_email_code(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"loginType": "verifyCode"})
    transport.json("POST", LOGIN_PATH, {"accessToken": "tok"})
    client = BambuCloudClient(transport)
    first = await client.login(ACCOUNT, "secret")
    assert first.needs == "verify_code"
    assert first.token is None
    second = await client.login_with_code(ACCOUNT, " 123456 ")
    assert second.token == "tok"
    assert transport.calls[1]["json"] == {"account": ACCOUNT, "code": "123456"}


async def test_wrong_email_code(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"code": 2, "error": "bad code"}, status=400)
    with pytest.raises(BambuCodeError) as info:
        await BambuCloudClient(transport).login_with_code(ACCOUNT, "000000")
    assert info.value.key == "invalid_code"


async def test_expired_email_code_requests_new_one(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"code": 1, "error": "expired"}, status=400)
    transport.json("POST", SEND_CODE_PATH, {})
    with pytest.raises(BambuCodeExpired):
        await BambuCloudClient(transport).login_with_code(ACCOUNT, "000000")
    assert transport.calls[1]["json"] == {"email": ACCOUNT, "type": "codeLogin"}


async def test_send_code_failure(transport: FakeTransport) -> None:
    transport.json("POST", SEND_CODE_PATH, {}, status=400)
    with pytest.raises(BambuError):
        await BambuCloudClient(transport).send_code(ACCOUNT)


async def test_code_login_without_token(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"loginType": "verifyCode"})
    with pytest.raises(BambuCodeError):
        await BambuCloudClient(transport).login_with_code(ACCOUNT, "000000")


async def test_login_with_tfa(transport: FakeTransport) -> None:
    transport.json("POST", LOGIN_PATH, {"loginType": "tfa", "tfaKey": "key-1"})
    transport.add("GET", CSRF_PATH, Response(204, "", {"bbl_csrf_token": "csrf-1"}))
    transport.add("POST", TFA_PATH, Response(200, "{}", {"token": "tok"}))
    client = BambuCloudClient(transport)
    first = await client.login(ACCOUNT, "secret")
    assert first.needs == "tfa"
    assert first.tfa_key == "key-1"
    second = await client.login_with_tfa("key-1", "654321")
    assert second.token == "tok"
    tfa = transport.calls[2]
    assert tfa["url"] == f"https://bambulab.com{TFA_PATH}"
    assert tfa["json"] == {"tfaKey": "key-1", "tfaCode": "654321"}
    assert tfa["headers"]["x-bbl-csrf-token"] == "csrf-1"
    assert tfa["headers"]["Cookie"] == "bbl_csrf_token=csrf-1"


async def test_tfa_wrong_code(transport: FakeTransport) -> None:
    transport.add("GET", CSRF_PATH, Response(200, "", {"bbl_csrf_token": "csrf-1"}))
    transport.add("POST", TFA_PATH, Response(400, "{}"))
    with pytest.raises(BambuCodeError):
        await BambuCloudClient(transport).login_with_tfa("key-1", "000000")


async def test_tfa_without_csrf(transport: FakeTransport) -> None:
    transport.add("GET", CSRF_PATH, Response(200, ""))
    with pytest.raises(BambuError):
        await BambuCloudClient(transport).login_with_tfa("key-1", "000000")


@pytest.mark.parametrize(
    ("status", "text", "error"),
    [
        (400, '{"code": 2, "error": "Incorrect password"}', BambuAuthError),
        (200, '{"message": "nothing useful"}', BambuAuthError),
        (200, "not json", BambuAuthError),
        (403, "<html>Attention Required! | Cloudflare</html>", BambuBlocked),
        (429, "slow down", BambuRateLimited),
        (502, "bad gateway", BambuError),
    ],
)
async def test_login_errors(
    transport: FakeTransport, status: int, text: str, error: type[BambuError]
) -> None:
    transport.add("POST", LOGIN_PATH, Response(status, text))
    with pytest.raises(error):
        await BambuCloudClient(transport).login(ACCOUNT, "secret")


def _hit(spool_id: int, **extra) -> dict:
    return {
        "id": spool_id,
        "color": "#101410FF",
        "filamentType": "PLA",
        "netWeight": 931,
        "totalNetWeight": 1000,
        **extra,
    }


async def test_fetch_spools_paginates(transport: FakeTransport) -> None:
    transport.json(
        "GET", FILAMENT_PATH, {"total": 3, "hits": [_hit(1), _hit(2, color=None)]}
    )
    transport.json("GET", FILAMENT_PATH, {"total": 3, "hits": [_hit(3)]})
    client = BambuCloudClient(transport, token="tok")
    spools, raw = await client.fetch_spools()
    assert [s.id for s in spools] == [1, 3]
    assert spools[0].filament_id == ""
    assert len(raw) == 3
    assert transport.calls[0]["params"] == {"offset": "0", "limit": "100"}
    assert transport.calls[1]["params"] == {"offset": "2", "limit": "100"}
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_fetch_spools_stops_on_empty_page(transport: FakeTransport) -> None:
    transport.json("GET", FILAMENT_PATH, {"total": 50, "hits": []})
    spools, _ = await BambuCloudClient(transport, token="tok").fetch_spools()
    assert spools == []
    assert len(transport.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_spools_rejected_token(
    transport: FakeTransport, status: int
) -> None:
    transport.add("GET", FILAMENT_PATH, Response(status, '{"message": "expired"}'))
    with pytest.raises(BambuAuthError):
        await BambuCloudClient(transport, token="tok").fetch_spools()


async def test_fetch_spools_without_token(transport: FakeTransport) -> None:
    with pytest.raises(BambuAuthError):
        await BambuCloudClient(transport).fetch_spools()
    assert transport.calls == []


async def test_unexpected_list_shape(transport: FakeTransport) -> None:
    transport.json("GET", FILAMENT_PATH, {"total": 1, "hits": "nope"})
    with pytest.raises(BambuError):
        await BambuCloudClient(transport, token="tok").fetch_spools()


async def test_close(transport: FakeTransport) -> None:
    await BambuCloudClient(transport).close()
    assert transport.closed


def test_parse_spool_fields() -> None:
    spool = parse_spool(
        _hit(
            7,
            note="  mat  ",
            filamentName="PLA Matte",
            filamentVendor="Bambu Lab",
            inPrinter=True,
            updatedAt=1700000000,
        )
    )
    assert spool is not None
    assert spool.id == 7
    assert spool.color == "#101410"
    assert spool.material == "PLA"
    assert spool.note == "mat"
    assert spool.net_weight == 931
    assert spool.total_weight == 1000
    assert spool.in_printer
    assert spool.updated_at == 1700000000


@pytest.mark.parametrize(
    "hit",
    [
        {"color": "#FFFFFF"},
        {"id": "x", "color": "#FFFFFF"},
        {"id": 1, "color": "white"},
    ],
)
def test_parse_spool_rejects(hit: dict) -> None:
    assert parse_spool(hit) is None


def test_parse_spool_bad_numbers() -> None:
    spool = parse_spool(
        {"id": "5", "color": "FFFFFF", "netWeight": "?", "totalNetWeight": -3}
    )
    assert spool is not None
    assert spool.net_weight == 0
    assert spool.total_weight == 0
    assert spool.material == "UNKNOWN"


def test_token_user() -> None:
    assert token_user(make_jwt(1, username="u_123")) == "u_123"
    assert token_user(make_jwt(1)) is None
    assert token_user("opaque") is None


def test_token_expiry() -> None:
    assert token_expiry(make_jwt(1900000000)) == 1900000000
    fallback = token_expiry("opaque-token")
    assert fallback == pytest.approx(time.time() + 90 * 86400, abs=5)


class _FakeCurlResponse:
    status_code = 200
    text = '{"accessToken": "tok"}'
    cookies: ClassVar[dict[str, object]] = {"token": "c", "ignored": 1}


async def test_curl_transport() -> None:
    session = AsyncMock()
    session.request.return_value = _FakeCurlResponse()
    with patch(
        "custom_components.filament_stock.bambu_api.AsyncSession", return_value=session
    ) as factory:
        transport = CurlTransport()
        resp = await transport.request(
            "POST", "https://api.bambulab.com/x", json_body={}
        )
        await transport.request("GET", "https://api.bambulab.com/y")
        await transport.close()
    factory.assert_called_once_with(impersonate="chrome", timeout=20.0)
    assert resp.status == 200
    assert resp.cookies == {"token": "c"}
    assert session.request.call_args.kwargs["allow_redirects"] is False
    session.close.assert_awaited_once()


async def test_curl_transport_network_error() -> None:
    session = AsyncMock()
    session.request.side_effect = RequestsError("boom")
    with (
        patch(
            "custom_components.filament_stock.bambu_api.AsyncSession",
            return_value=session,
        ),
        pytest.raises(BambuError),
    ):
        await CurlTransport().request("GET", "https://api.bambulab.com/x")


def test_parse_spool_reads_the_filament_profile_id() -> None:
    spool = parse_spool(_hit(1, filamentId="GFA01"))
    assert spool is not None
    assert spool.filament_id == "GFA01"
