"""Read-only client for the Bambu Lab cloud filament list.

No Home Assistant imports. The API is unofficial. Login flow and endpoints
follow the public research by Timmes123 (ha-bambu-filaments, MIT) and
OpenBambuAPI. Cloudflare blocks plain Python HTTP clients on login, so the
default transport uses curl_cffi with a browser TLS fingerprint. The transport
sits behind a small protocol so it can be swapped and mocked.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession, RequestsError

from .colors import normalize_hex
from .models import Spool
from .parser import normalize_material

_LOGGER = logging.getLogger(__name__)

API_HOST = "https://api.bambulab.com"
WEB_HOST = "https://bambulab.com"
ALLOWED_HOSTS = frozenset({"api.bambulab.com", "bambulab.com"})

LOGIN_PATH = "/v1/user-service/user/login"
SEND_CODE_PATH = "/v1/user-service/user/sendemail/code"
FILAMENT_PATH = "/v1/design-user-service/my/filament/v2"
CSRF_PATH = "/api/csrf"
TFA_PATH = "/api/sign-in/tfa"

PAGE_SIZE = 100
MAX_PAGES = 50
TIMEOUT = 20.0
DEFAULT_TOKEN_LIFETIME = 90 * 24 * 3600

type Method = Literal["GET", "POST"]


class BambuError(Exception):
    """Base error. `key` is the config flow error key."""

    key = "cannot_connect"


class BambuAuthError(BambuError):
    """Wrong account or password, or the token was rejected."""

    key = "invalid_auth"


class BambuCodeError(BambuError):
    """Wrong or expired verification code."""

    key = "invalid_code"


class BambuCodeExpired(BambuCodeError):
    """The code expired. A new one was requested."""

    key = "code_expired"


class BambuBlocked(BambuError):
    """Cloudflare blocked the request."""

    key = "cloudflare_blocked"


class BambuRateLimited(BambuError):
    key = "rate_limited"


@dataclass(frozen=True, slots=True)
class Response:
    """Transport-neutral response."""

    status: int
    text: str
    cookies: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


class Transport(Protocol):
    """Minimal HTTP transport."""

    async def request(
        self,
        method: Method,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response: ...

    async def close(self) -> None: ...


class CurlTransport:
    """curl_cffi transport with a Chrome fingerprint. No redirects."""

    def __init__(self) -> None:
        self._session: AsyncSession | None = None

    async def request(
        self,
        method: Method,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome", timeout=TIMEOUT)
        try:
            resp = await self._session.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=headers,
                allow_redirects=False,
            )
        except RequestsError as err:
            raise BambuError(type(err).__name__) from err
        return Response(
            status=resp.status_code,
            text=resp.text,
            cookies={k: v for k, v in resp.cookies.items() if isinstance(v, str)},
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a login step."""

    token: str | None = None
    needs: str | None = None  # 'verify_code' or 'tfa'
    tfa_key: str | None = None


def _jwt_claims(token: str) -> dict[str, Any]:
    """Unverified JWT payload. Only used to read 'exp' and 'username'."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except IndexError, TypeError, ValueError:
        return {}
    return claims if isinstance(claims, dict) else {}


def token_expiry(token: str) -> int:
    """JWT 'exp' claim (unix seconds), or 90 days from now if not readable."""
    try:
        return int(_jwt_claims(token)["exp"])
    except KeyError, TypeError, ValueError:
        return int(time.time()) + DEFAULT_TOKEN_LIFETIME


def token_user(token: str) -> str | None:
    """Stable Bambu user name ('u_<digits>') from the token, if present."""
    username = _jwt_claims(token).get("username")
    return username if isinstance(username, str) and username else None


def parse_spool(hit: dict[str, Any]) -> Spool | None:
    """Cloud spool record to Spool. None when it lacks an id or color."""
    try:
        spool_id = int(hit["id"])
    except KeyError, TypeError, ValueError:
        return None
    color = normalize_hex(str(hit.get("color") or ""))
    if color is None:
        return None

    def number(key: str) -> float:
        try:
            return max(0.0, float(hit.get(key) or 0))
        except TypeError, ValueError:
            return 0.0

    updated = hit.get("updatedAt")
    return Spool(
        id=spool_id,
        material=normalize_material(str(hit.get("filamentType") or "")),
        color=color,
        note=str(hit.get("note") or "").strip()[:100],
        name=str(hit.get("filamentName") or "")[:100],
        vendor=str(hit.get("filamentVendor") or "")[:100],
        net_weight=number("netWeight"),
        total_weight=number("totalNetWeight"),
        in_printer=bool(hit.get("inPrinter")),
        updated_at=int(updated) if isinstance(updated, int | float) else None,
        filament_id=str(hit.get("filamentId") or "").strip()[:32],
    )


class BambuCloudClient:
    """Login and read the filament list. Never stores the password."""

    def __init__(self, transport: Transport, token: str | None = None) -> None:
        self._transport = transport
        self.token = token

    async def _call(
        self,
        method: Method,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool = False,
    ) -> Response:
        if urlparse(url).hostname not in ALLOWED_HOSTS:
            raise BambuError("host not allowed")
        all_headers = dict(headers or {})
        if auth:
            if not self.token:
                raise BambuAuthError
            all_headers["Authorization"] = f"Bearer {self.token}"
        resp = await self._transport.request(
            method, url, json_body=json_body, params=params, headers=all_headers
        )
        if resp.status in (403, 429) and "cloudflare" in resp.text.lower():
            raise BambuBlocked
        if resp.status == 429:
            raise BambuRateLimited
        if auth and resp.status in (401, 403):
            raise BambuAuthError
        if resp.status >= 500:
            raise BambuError(f"server status {resp.status}")
        return resp

    async def login(self, account: str, password: str) -> LoginResult:
        """Password login. May ask for an email code or a 2FA code."""
        resp = await self._call(
            "POST",
            f"{API_HOST}{LOGIN_PATH}",
            json_body={"account": account, "password": password, "apiError": ""},
        )
        return self._login_result(resp)

    async def login_with_code(self, account: str, code: str) -> LoginResult:
        """Second step after loginType 'verifyCode'.

        Bambu emails the code on the password login. On an expired code
        (error code 1) a new one is requested, as ha-bambulab does.
        """
        resp = await self._call(
            "POST",
            f"{API_HOST}{LOGIN_PATH}",
            json_body={"account": account, "code": code.strip()},
        )
        body = resp.json()
        if resp.status == 400 and isinstance(body, dict) and body.get("code") == 1:
            await self.send_code(account)
            raise BambuCodeExpired
        result = self._login_result(resp, code_step=True)
        if result.token is None:
            raise BambuCodeError
        return result

    async def send_code(self, account: str) -> None:
        """Ask Bambu to email a new login code."""
        resp = await self._call(
            "POST",
            f"{API_HOST}{SEND_CODE_PATH}",
            json_body={"email": account, "type": "codeLogin"},
        )
        if resp.status >= 400:
            raise BambuError(f"send code status {resp.status}")

    async def login_with_tfa(self, tfa_key: str, code: str) -> LoginResult:
        """Second step after loginType 'tfa'. The token comes back as a cookie."""
        csrf = await self._call("GET", f"{WEB_HOST}{CSRF_PATH}")
        csrf_token = csrf.cookies.get("bbl_csrf_token")
        if csrf.status not in (200, 204) or not csrf_token:
            raise BambuError("no csrf token")
        resp = await self._call(
            "POST",
            f"{WEB_HOST}{TFA_PATH}",
            json_body={"tfaKey": tfa_key, "tfaCode": code.strip()},
            headers={
                "x-bbl-csrf-token": csrf_token,
                "Cookie": f"bbl_csrf_token={csrf_token}",
            },
        )
        token = resp.cookies.get("token")
        if resp.status >= 400 or not token:
            raise BambuCodeError
        self.token = token
        return LoginResult(token=token)

    def _login_result(self, resp: Response, code_step: bool = False) -> LoginResult:
        body = resp.json()
        if resp.status >= 400 or not isinstance(body, dict):
            raise BambuCodeError if code_step else BambuAuthError
        if token := body.get("accessToken"):
            self.token = str(token)
            return LoginResult(token=self.token)
        login_type = body.get("loginType")
        if login_type == "verifyCode":
            return LoginResult(needs="verify_code")
        if login_type == "tfa" and body.get("tfaKey"):
            return LoginResult(needs="tfa", tfa_key=str(body["tfaKey"]))
        raise BambuCodeError if code_step else BambuAuthError

    async def fetch_spools(self) -> tuple[list[Spool], list[dict[str, Any]]]:
        """All spools, paginated. Also returns the raw records for diagnostics."""
        raw: list[dict[str, Any]] = []
        offset = 0
        for _ in range(MAX_PAGES):
            resp = await self._call(
                "GET",
                f"{API_HOST}{FILAMENT_PATH}",
                params={"offset": str(offset), "limit": str(PAGE_SIZE)},
                auth=True,
            )
            body = resp.json()
            if resp.status != 200 or not isinstance(body, dict):
                raise BambuError(f"filament list status {resp.status}")
            hits = body.get("hits") or []
            if not isinstance(hits, list):
                raise BambuError("unexpected filament list")
            raw.extend(h for h in hits if isinstance(h, dict))
            offset += len(hits)
            total = body.get("total")
            if not hits or not isinstance(total, int) or offset >= total:
                break
        spools = [s for s in (parse_spool(h) for h in raw) if s is not None]
        if len(spools) != len(raw):
            _LOGGER.debug(
                "Skipped %d spool records without id or color", len(raw) - len(spools)
            )
        return spools, raw

    async def close(self) -> None:
        await self._transport.close()
