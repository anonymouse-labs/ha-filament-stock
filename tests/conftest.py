"""Shared fixtures."""

from __future__ import annotations

import base64
from collections.abc import Callable, Generator
import json
import time
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.filament_stock.bambu_api import Response
from custom_components.filament_stock.const import (
    CONF_ACCOUNT,
    CONF_FOLDER_ID,
    CONF_SERVICE_ACCOUNT,
    CONF_TOKEN,
    CONF_TOKEN_EXPIRES,
    DOMAIN,
)

FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
SA_EMAIL = "reader@example-project.iam.gserviceaccount.com"
ACCOUNT = "maker@example.com"


def make_jwt(exp: float, username: str | None = None) -> str:
    """Unsigned JWT-shaped token with 'exp' and optional 'username' claims."""

    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    claims: dict[str, Any] = {"exp": int(exp)}
    if username:
        claims["username"] = username
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


TOKEN = make_jwt(time.time() + 80 * 86400)

Handler = Response | Callable[..., Response]


class FakeTransport:
    """Bambu transport stand-in: routes by method and path, records calls."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Handler]] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def add(self, method: str, path: str, *handlers: Handler) -> None:
        self.routes.setdefault((method, path), []).extend(handlers)

    def json(self, method: str, path: str, body: Any, status: int = 200, **kw: Any):
        self.add(method, path, Response(status, json.dumps(body), **kw))

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        path = urlparse(url).path
        call = {
            "method": method,
            "url": url,
            "json": json_body,
            "params": params,
            "headers": headers or {},
        }
        self.calls.append(call)
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"no fake route for {method} {path}")
        handler = queue.pop(0) if len(queue) > 1 else queue[0]
        return handler(**call) if callable(handler) else handler

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every HA test."""
    return


@pytest.fixture(autouse=True)
def no_real_bambu() -> Generator[None]:
    """curl_cffi bypasses pytest-socket: fail any test that would reach the cloud."""
    with patch(
        "custom_components.filament_stock.bambu_api.AsyncSession",
        side_effect=AssertionError("real Bambu request in a test"),
    ):
        yield


@pytest.fixture
def bambu() -> Generator[FakeTransport]:
    """Replace the curl transport everywhere. Empty spool list by default."""
    fake = FakeTransport()
    with (
        patch("custom_components.filament_stock.CurlTransport", return_value=fake),
        patch(
            "custom_components.filament_stock.config_flow.CurlTransport",
            return_value=fake,
        ),
    ):
        yield fake


@pytest.fixture(scope="session")
def service_account_json() -> str:
    """A throwaway service account key, generated per test session."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "example-project",
            "private_key_id": "0" * 40,
            "private_key": pem,
            "client_email": SA_EMAIL,
            "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )


@pytest.fixture
def entry(hass: HomeAssistant, service_account_json: str) -> MockConfigEntry:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT,
        data={
            CONF_ACCOUNT: ACCOUNT,
            CONF_TOKEN: TOKEN,
            CONF_TOKEN_EXPIRES: int(time.time() + 80 * 86400),
            CONF_SERVICE_ACCOUNT: service_account_json,
            CONF_FOLDER_ID: FOLDER_ID,
        },
    )
    config_entry.add_to_hass(hass)
    return config_entry


@pytest.fixture
def mock_token() -> Generator[None]:
    """Skip the real OAuth token exchange."""
    with patch(
        "custom_components.filament_stock.drive.DriveClient._token",
        return_value="test-token",
    ):
        yield


@pytest.fixture
def no_backoff() -> Generator[None]:
    """Do not sleep between retries."""
    with patch("custom_components.filament_stock.drive.asyncio.sleep"):
        yield
