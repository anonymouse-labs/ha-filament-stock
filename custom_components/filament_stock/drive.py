"""Read-only Google Drive client using a service account.

No Home Assistant imports. Only Google hosts are contacted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials

_LOGGER = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_API = "https://www.googleapis.com/drive/v3"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL, not a secret
FOLDER_MIME = "application/vnd.google-apps.folder"
ALLOWED_HOST_SUFFIXES = (".googleapis.com", ".googleusercontent.com")

MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_LISTED_FILES = 1000
REQUEST_TIMEOUT = ClientTimeout(total=30)
DOWNLOAD_TIMEOUT = ClientTimeout(total=300, sock_read=60)
RETRIES = 3

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]+)")
_API_DISABLED_REASONS = {"accessNotConfigured", "SERVICE_DISABLED"}
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class DriveError(Exception):
    """Base Drive error. `key` is the config flow error key."""

    key = "cannot_connect"


class DriveInvalidJson(DriveError):
    key = "invalid_json"


class DriveInvalidServiceAccount(DriveError):
    key = "invalid_service_account"


class DriveInvalidFolderId(DriveError):
    key = "invalid_folder_id"


class DriveApiDisabled(DriveError):
    key = "drive_api_disabled"


class DriveNotFound(DriveError):
    key = "folder_not_found"


class DriveForbidden(DriveError):
    key = "forbidden"


class DriveNotAFolder(DriveError):
    key = "not_a_folder"


class DriveRateLimited(DriveError):
    key = "rate_limited"


class DriveFileTooLarge(DriveError):
    key = "file_too_large"


@dataclass(frozen=True, slots=True)
class DriveFile:
    """One listed file."""

    id: str
    name: str
    stamp: str  # md5 checksum, or modified time when Drive gives none
    size: int | None


def parse_folder_id(value: str) -> str:
    """Accept a bare folder ID or a drive.google.com folder URL."""
    value = value.strip()
    if match := _FOLDER_URL_RE.search(value):
        value = match.group(1)
    if not _ID_RE.match(value):
        raise DriveInvalidFolderId
    return value


def _check_id(value: str) -> str:
    """IDs end up in URLs and queries: only allow Drive's ID alphabet."""
    if not _ID_RE.match(value):
        raise DriveInvalidFolderId
    return value


def parse_service_account(raw: str) -> dict[str, Any]:
    """Validate the pasted service account JSON."""
    try:
        info = json.loads(raw)
    except ValueError as err:
        raise DriveInvalidJson from err
    if not isinstance(info, dict):
        raise DriveInvalidJson
    if info.get("type") != "service_account" or not all(
        isinstance(info.get(k), str) and info[k]
        for k in ("client_email", "private_key", "token_uri")
    ):
        raise DriveInvalidServiceAccount
    if info["token_uri"] != TOKEN_URI:
        raise DriveInvalidServiceAccount
    return info


def _error_reason(body: Any) -> str:
    try:
        error = body["error"]
        details = error.get("errors") or [{}]
        reason = details[0].get("reason") or ""
        if not reason:
            for item in error.get("details", []):
                reason = item.get("reason") or reason
        return str(reason)
    except KeyError, TypeError, AttributeError, IndexError:
        return ""


class DriveClient:
    """List and download files from a folder shared with a service account."""

    def __init__(self, session: ClientSession, service_account: str) -> None:
        self._session = session
        info = parse_service_account(service_account)
        self.email: str = info["client_email"]
        try:
            self._creds = Credentials.from_service_account_info(
                info, scopes=[DRIVE_SCOPE]
            )
        except (ValueError, KeyError) as err:
            raise DriveInvalidServiceAccount from err
        self._token_lock = asyncio.Lock()

    def _refresh(self) -> None:
        self._creds.refresh(Request())

    async def _token(self) -> str:
        async with self._token_lock:
            if not self._creds.valid:
                loop = asyncio.get_running_loop()
                try:
                    await loop.run_in_executor(None, self._refresh)
                except TransportError as err:
                    raise DriveError from err
                except GoogleAuthError as err:
                    # Refresh errors may echo the assertion: do not log details.
                    raise DriveInvalidServiceAccount from err
                except OSError as err:
                    raise DriveError from err
        return str(self._creds.token)

    async def _request(
        self, url: str, params: dict[str, str], client_timeout: ClientTimeout
    ) -> ClientResponse:
        """GET with retry on rate limits and server errors."""
        for attempt in range(RETRIES):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            try:
                resp = await self._session.get(
                    url, params=params, headers=headers, timeout=client_timeout
                )
            except (TimeoutError, ClientError) as err:
                if attempt == RETRIES - 1:
                    raise DriveError from err
                await asyncio.sleep(2**attempt)
                continue
            host = urlparse(str(resp.url)).hostname or ""
            if not host.endswith(ALLOWED_HOST_SUFFIXES):
                resp.release()
                raise DriveError(f"unexpected host {host}")
            if resp.status == 200:
                return resp
            try:
                body = await resp.json(content_type=None)
            except ValueError, ClientError:
                body = None
            finally:
                resp.release()
            reason = _error_reason(body)
            retry = resp.status in (429, 500, 502, 503, 504) or (
                resp.status == 403 and reason in _RATE_LIMIT_REASONS
            )
            if retry and attempt < RETRIES - 1:
                await asyncio.sleep(2**attempt)
                continue
            raise self._status_error(resp.status, reason)
        raise DriveError  # pragma: no cover

    @staticmethod
    def _status_error(status: int, reason: str) -> DriveError:
        if status == 404:
            return DriveNotFound()
        if reason in _API_DISABLED_REASONS:
            return DriveApiDisabled()
        if status == 429 or reason in _RATE_LIMIT_REASONS:
            return DriveRateLimited()
        if status == 401:
            return DriveInvalidServiceAccount()
        if status == 403:
            return DriveForbidden()
        return DriveError(f"drive status {status}")

    async def _json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        resp = await self._request(url, params, REQUEST_TIMEOUT)
        try:
            body = await resp.json(content_type=None)
        except (ValueError, ClientError) as err:
            raise DriveError from err
        finally:
            resp.release()
        if not isinstance(body, dict):
            raise DriveError("unexpected response")
        return body

    async def get_folder(self, folder_id: str) -> str:
        """Return the folder name. Raises when there is no access.

        files.list on an unshared folder returns an empty list, not an error,
        so the folder itself is checked first.
        """
        body = await self._json(
            f"{DRIVE_API}/files/{_check_id(folder_id)}",
            {"fields": "id,name,mimeType", "supportsAllDrives": "true"},
        )
        if body.get("mimeType") != FOLDER_MIME:
            raise DriveNotAFolder
        return str(body.get("name", ""))

    async def list_folder(self, folder_id: str) -> list[DriveFile]:
        """Files (not folders) directly in the folder."""
        _check_id(folder_id)
        files: list[DriveFile] = []
        page_token = ""
        while len(files) < MAX_LISTED_FILES:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false "
                f"and mimeType != '{FOLDER_MIME}'",
                "fields": "nextPageToken,files(id,name,md5Checksum,modifiedTime,size)",
                "pageSize": "200",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            body = await self._json(f"{DRIVE_API}/files", params)
            for item in body.get("files", []):
                try:
                    size = int(item["size"]) if "size" in item else None
                    files.append(
                        DriveFile(
                            id=str(item["id"]),
                            name=str(item["name"])[:255],
                            stamp=str(
                                item.get("md5Checksum") or item.get("modifiedTime", "")
                            ),
                            size=size,
                        )
                    )
                except KeyError, TypeError, ValueError:
                    continue
            page_token = str(body.get("nextPageToken") or "")
            if not page_token:
                break
        return files[:MAX_LISTED_FILES]

    async def download(self, file_id: str, limit: int = MAX_DOWNLOAD_BYTES) -> bytes:
        """Download one file, stopping at `limit` bytes."""
        resp = await self._request(
            f"{DRIVE_API}/files/{_check_id(file_id)}",
            {"alt": "media", "supportsAllDrives": "true"},
            DOWNLOAD_TIMEOUT,
        )
        try:
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                raise DriveFileTooLarge
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.content.iter_chunked(64 * 1024):
                total += len(chunk)
                if total > limit:
                    raise DriveFileTooLarge
                chunks.append(chunk)
        except (TimeoutError, ClientError) as err:
            raise DriveError from err
        finally:
            resp.release()
        return b"".join(chunks)
