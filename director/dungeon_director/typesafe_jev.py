"""Direct TypeSafe Jev provider adapter.

This transport calls TypeSafe's System One endpoint directly while reusing the
same bounded Jev question set and deterministic room composition as the
Cloudflare adapter. Credentials stay server-side and upstream response text is
never copied into errors or logs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from dungeon_director.cloudflare_jev import (
    HttpxJevTransport,
    JevTransport,
    JevTransportRequest,
    JevTransportResponse,
    build_jev_questions,
    build_jev_state,
    compose_jev_room,
    decode_jev_payload,
)
from dungeon_director.contracts import GenerationRequest
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.providers import (
    MODEL_ID_RE,
    DungeonDirectorProvider,
    ProviderAvailability,
    ProviderResult,
)

__all__ = [
    "DEFAULT_TYPESAFE_JEV_API_URL",
    "DEFAULT_TYPESAFE_JEV_MODEL",
    "TYPESAFE_JEV_PROVIDER_ID",
    "TypeSafeJevConfig",
    "TypeSafeJevProvider",
]

TYPESAFE_JEV_PROVIDER_ID = "typesafe-jev"
DEFAULT_TYPESAFE_JEV_MODEL = "jev-latest"
DEFAULT_TYPESAFE_JEV_API_URL = "https://api.typesafe.ai/v1/systemone"

_KEY_RE = re.compile(r"[\x21-\x7e]{1,4096}")
_URL_RE = re.compile(r"[\x21-\x7e]{1,2048}")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True, slots=True)
class TypeSafeJevConfig:
    """Server-side direct TypeSafe configuration with a redacted API key."""

    model: str = DEFAULT_TYPESAFE_JEV_MODEL
    api_url: str = DEFAULT_TYPESAFE_JEV_API_URL
    api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.api_key and not _KEY_RE.fullmatch(self.api_key):
            raise DirectorConfigError(
                "TYPESAFE_API_KEY must be 1-4096 printable ASCII characters without spaces"
            )
        if not isinstance(self.model, str) or MODEL_ID_RE.fullmatch(self.model) is None:
            raise DirectorConfigError("TYPESAFE_JEV_MODEL must be a valid model id")
        if not isinstance(self.api_url, str) or _URL_RE.fullmatch(self.api_url) is None:
            raise DirectorConfigError(
                "TYPESAFE_JEV_API_URL must not contain whitespace or control characters"
            )
        try:
            parsed = urlsplit(self.api_url)
            _validated_port = parsed.port
        except ValueError:
            raise DirectorConfigError(
                "TYPESAFE_JEV_API_URL must be an absolute http(s) URL"
            ) from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DirectorConfigError("TYPESAFE_JEV_API_URL must be an absolute http(s) URL")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DirectorConfigError(
                "TYPESAFE_JEV_API_URL must not contain credentials, a query or a fragment"
            )
        if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
            raise DirectorConfigError(
                "TYPESAFE_JEV_API_URL must use https except for a loopback test server"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TypeSafeJevConfig:
        env = os.environ if environ is None else environ

        def read(name: str) -> str:
            value = env.get(name)
            return value.strip() if isinstance(value, str) else ""

        return cls(
            model=read("TYPESAFE_JEV_MODEL") or DEFAULT_TYPESAFE_JEV_MODEL,
            api_url=read("TYPESAFE_JEV_API_URL").rstrip("/") or DEFAULT_TYPESAFE_JEV_API_URL,
            api_key=read("TYPESAFE_API_KEY"),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


class TypeSafeJevProvider(DungeonDirectorProvider):
    """Decide rooms with Jev through TypeSafe's direct System One API."""

    def __init__(
        self,
        config: TypeSafeJevConfig,
        transport: JevTransport | None = None,
    ) -> None:
        self._config = config
        self._injected_transport = transport
        self._transport: JevTransport | None = transport
        self.provider_id = TYPESAFE_JEV_PROVIDER_ID
        self.models = (config.model,)
        self.default_model = config.model

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        transport: JevTransport | None = None,
    ) -> TypeSafeJevProvider:
        return cls(TypeSafeJevConfig.from_env(environ), transport)

    @property
    def availability(self) -> ProviderAvailability:
        if self._config.has_credentials:
            return ProviderAvailability(True)
        return ProviderAvailability(
            False,
            "typesafe-jev is registered but not configured: set TYPESAFE_API_KEY to enable it",
        )

    def __repr__(self) -> str:
        return (
            f"TypeSafeJevProvider(model={self._config.model!r}, "
            f"configured={self._config.has_credentials})"
        )

    async def aclose(self) -> None:
        if self._transport is not None and self._transport is not self._injected_transport:
            closer = getattr(self._transport, "aclose", None)
            if closer is not None:
                await closer()
        self._transport = self._injected_transport

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        started = time.perf_counter()
        if not self._config.has_credentials:
            raise ProviderError(
                ErrorKind.PROVIDER_ERROR, "typesafe-jev has no credentials configured"
            )
        if model not in self.models:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, f"model {model!r} is not configured")

        body = {
            "state": build_jev_state(request),
            "model": model,
            "questions": build_jev_questions(request),
        }
        if self._transport is None:
            self._transport = HttpxJevTransport()
        transport_request = JevTransportRequest(
            method="POST",
            url=self._config.api_url,
            headers={
                **self._config.authorization_header(),
                "Content-Type": "application/json",
            },
            json_body=body,
        )

        try:
            response = await self._transport.send(transport_request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _transport_error(exc) from exc

        if response.status_code != 200:
            raise _http_status_error(response.status_code)

        payload = _decode_success(response)
        answers, jev_model, usage = decode_jev_payload(payload)
        room, metadata = compose_jev_room(request, answers, model)
        metadata.update(
            {
                "jev_model": jev_model,
                "upstream_http_status": 200,
                "adapter_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return ProviderResult(payload=room, usage=usage, provider_metadata=metadata)


def _transport_error(exc: Exception) -> ProviderError:
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                ErrorKind.PROVIDER_TIMEOUT, "typesafe request timed out at the transport layer"
            )
        if isinstance(exc, httpx.HTTPError):
            return ProviderError(
                ErrorKind.PROVIDER_ERROR,
                f"typesafe request failed at the transport layer ({type(exc).__name__})",
            )
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        pass
    return ProviderError(
        ErrorKind.PROVIDER_ERROR,
        f"jev transport raised {type(exc).__name__}",
    )


def _http_status_error(status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR,
            f"typesafe rejected the credentials (HTTP {status})",
        )
    if status == 429:
        return ProviderError(ErrorKind.RATE_LIMITED, "typesafe rate limit reached (HTTP 429)")
    if status in (400, 422):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR,
            f"typesafe rejected the request body (HTTP {status})",
        )
    if status == 529:
        return ProviderError(ErrorKind.PROVIDER_ERROR, "typesafe is overloaded (HTTP 529)")
    if status >= 500:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR,
            f"typesafe reported an upstream error (HTTP {status})",
        )
    return ProviderError(ErrorKind.PROVIDER_ERROR, f"typesafe returned HTTP {status}")


def _decode_success(response: JevTransportResponse) -> Any:
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            ErrorKind.INVALID_JSON, "typesafe response body was not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "typesafe response was not a JSON object")
    return body
