"""Shared publisher types and resilient HTTP behavior."""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_TIMEOUT = 30.0
MAX_ATTEMPTS = 3


class APIError(RuntimeError):
    """A human-readable remote API failure."""


class AuthenticationError(APIError):
    """Credentials could not be authenticated."""


class PublishError(APIError):
    """A post could not be published."""


@dataclass(slots=True)
class PostData(abc.ABC):
    """Platform-neutral post content.

    Media entries may be local paths. Publishers that support remote media may
    also accept public HTTP(S) URLs.
    """

    text: str = ""
    media: Sequence[str | Path] = field(default_factory=tuple)
    scheduled_at: str | None = None

    @abc.abstractmethod
    def validate(self) -> None:
        """Raise ValueError when the post is invalid for its platform."""


class SocialPlatform(abc.ABC):
    """Base class for asynchronous social platform publishers."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), proxy=proxy, trust_env=trust_env
        )
        self._logger = logging.getLogger(f"{__name__}.{type(self).__name__}")

    async def __aenter__(self) -> SocialPlatform:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @abc.abstractmethod
    async def authenticate(self) -> Mapping[str, Any]:
        """Validate credentials and return non-secret account details."""

    @abc.abstractmethod
    async def publish(self, post: PostData) -> Mapping[str, Any]:
        """Publish a validated post and return the API response."""

    async def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Request with bounded retries, without logging query strings or data."""

        safe_url = _safe_url(url)
        response: httpx.Response | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self._logger.info("%s: %s %s (attempt %d)", operation, method, safe_url, attempt)
                _rewind_files(kwargs.get("files"))
                response = await self._client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise APIError(
                        f"{operation}: сеть недоступна после {MAX_ATTEMPTS} попыток "
                        f"({type(exc).__name__}). Проверьте интернет, VPN или прокси."
                    ) from exc
                self._logger.warning(
                    "%s network error for %s %s; retrying (%d/%d)",
                    operation,
                    method,
                    safe_url,
                    attempt,
                    MAX_ATTEMPTS,
                )
                await asyncio.sleep(2 ** (attempt - 1))
                continue

            if response.status_code != 429 and not 500 <= response.status_code < 600:
                return response
            if attempt < MAX_ATTEMPTS:
                self._logger.warning(
                    "%s returned HTTP %d for %s %s; retrying (%d/%d)",
                    operation,
                    response.status_code,
                    method,
                    safe_url,
                    attempt,
                    MAX_ATTEMPTS,
                )
                await asyncio.sleep(2 ** (attempt - 1))
                continue
            break

        assert response is not None
        raise APIError(
            f"{operation}: сервер не ответил после {MAX_ATTEMPTS} попыток, "
            f"HTTP {response.status_code} ({_response_message(response)})"
        )


def require_success(response: httpx.Response, operation: str) -> dict[str, Any]:
    """Decode a successful JSON object or raise a readable APIError."""

    try:
        payload = response.json()
    except ValueError as exc:
        if response.is_success:
            raise APIError(f"{operation} returned invalid JSON") from exc
        raise APIError(f"{operation} failed: HTTP {response.status_code}") from exc

    if not response.is_success:
        raise APIError(
            f"{operation} failed: HTTP {response.status_code} ({_payload_message(payload)})"
        )
    if not isinstance(payload, dict):
        raise APIError(f"{operation} returned an unexpected response")
    return payload


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path
    if path.startswith("/bot") and "/" in path[4:]:
        path = "/bot***/" + path.split("/", 2)[-1]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _rewind_files(files: Any) -> None:
    if not isinstance(files, Mapping):
        return
    for value in files.values():
        stream = value[1] if isinstance(value, tuple) and len(value) > 1 else value
        seek = getattr(stream, "seek", None)
        if callable(seek):
            seek(0)


def _response_message(response: httpx.Response) -> str:
    try:
        return _payload_message(response.json())
    except ValueError:
        return response.reason_phrase or "remote service error"


def _payload_message(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(
                error.get("message")
                or error.get("error_msg")
                or error.get("description")
                or "remote service error"
            )
        return str(payload.get("description") or payload.get("message") or error or "remote service error")
    return "remote service error"
