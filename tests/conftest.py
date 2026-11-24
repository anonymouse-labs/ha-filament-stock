"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Generator
import json
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest

FOLDER_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
SA_EMAIL = "reader@example-project.iam.gserviceaccount.com"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every HA test."""
    return


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
