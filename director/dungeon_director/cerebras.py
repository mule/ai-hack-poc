"""Cerebras Qwen provider adapter (issue #10).

Reaches the public shared Cerebras model ``qwen-3.8-27b`` (configurable) through
Cerebras Inference's OpenAI-compatible Chat Completions endpoint
(``POST {CEREBRAS_API_BASE_URL}/chat/completions``) and asks for a strict
JSON Schema structured output describing one :class:`RoomPlan`.

Design points:

* ``reasoning_effort="none"`` and a small ``max_completion_tokens`` cap keep
  latency and token cost low; ``temperature=0`` plus a fixed ``seed`` make the
  request as reproducible as the upstream allows (best effort, not a guarantee).
* The adapter returns the model's *raw* message content. It does not validate
  the plan: :class:`~dungeon_director.service.DirectorService` owns canonical
  ``RoomPlan`` validation, so a schema failure is reported by the same code path
  as every other provider and still carries the usage and metadata captured here.
* Standard trust model: one call is one HTTP attempt (no retries, redirects are
  not followed), no adapter-side timeout (the service owns the deadline and
  cancels the call), a hard response-body cap, and credentials never appear in
  errors, logs, ``repr`` or API responses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import JsonValue

from dungeon_director.contracts import (
    EnvironmentalTag,
    ExitDirection,
    ExitKind,
    GenerationRequest,
    RoomSize,
    RoomType,
    UsageStats,
)
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.providers import DungeonDirectorProvider, ProviderAvailability, ProviderResult

__all__ = [
    "CEREBRAS_PROVIDER_ID",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_CEREBRAS_MODEL",
    "DEFAULT_MAX_COMPLETION_TOKENS",
    "CerebrasConfig",
    "CerebrasProvider",
    "CerebrasTransport",
    "CerebrasTransportRequest",
    "CerebrasResponseTooLarge",
    "CerebrasTransportResponse",
    "HttpxCerebrasTransport",
]

CEREBRAS_PROVIDER_ID = "cerebras"
DEFAULT_CEREBRAS_MODEL = "qwen-3.8-27b"
DEFAULT_API_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MAX_COMPLETION_TOKENS = 512
#: A RoomPlan is a few hundred tokens at most; the ceiling stops a typo'd
#: CEREBRAS_MAX_COMPLETION_TOKENS from turning one room into an expensive call.
_MIN_COMPLETION_TOKENS = 64
_MAX_COMPLETION_TOKENS_CEILING = 2048

#: Deterministic sampling: greedy decoding with a pinned seed.
_TEMPERATURE = 0
_SEED = 1

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
#: Printable ASCII without whitespace: anything else cannot be a valid Bearer
#: token and would only surface later as an opaque transport error.
_API_KEY_RE = re.compile(r"^[\x21-\x7e]{1,4096}$")
_MAX_RESPONSE_BODY_BYTES = 1_048_576
_LOOPBACK_HOSTS = {"localhost"}
_META_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_META_MAX_KEYS = 24

#: Strict-mode schema (every object closed, every property required). Enum values
#: come from the contract so the two cannot drift; numeric and length bounds are
#: stated in descriptions because strict mode does not enforce them, and the
#: director validates the returned plan against the full contract anyway.
_ROOM_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "room_id": {
            "type": "string",
            "description": "Identifier for the room, e.g. room-1",
        },
        "depth": {
            "type": "integer",
            "description": "Dungeon depth matching state.depth",
        },
        "room_type": {
            "type": "string",
            "enum": [member.value for member in RoomType],
        },
        "size": {
            "type": "string",
            "enum": [member.value for member in RoomSize],
        },
        "danger": {
            "type": "integer",
            "description": "Danger level from 1 to 5",
        },
        "enemy_density": {
            "type": "number",
            "description": "Enemy density between 0.0 and 1.0",
        },
        "loot_density": {
            "type": "number",
            "description": "Loot density between 0.0 and 1.0",
        },
        "secret_probability": {
            "type": "number",
            "description": "Secret probability between 0.0 and 1.0",
        },
        "has_secret": {
            "type": "boolean",
            "description": "True if room contains a secret (requires secret_probability > 0)",
        },
        "exits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": [member.value for member in ExitDirection],
                    },
                    "kind": {
                        "type": "string",
                        "enum": [member.value for member in ExitKind],
                    },
                    "locked": {
                        "type": "boolean",
                    },
                },
                "required": ["direction", "kind", "locked"],
                "additionalProperties": False,
            },
        },
        "environmental_tags": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [member.value for member in EnvironmentalTag],
            },
        },
        "description": {
            "type": "string",
            "description": "Flavor text description, max 200 chars",
        },
    },
    "required": [
        "room_id",
        "depth",
        "room_type",
        "size",
        "danger",
        "enemy_density",
        "loot_density",
        "secret_probability",
        "has_secret",
        "exits",
        "environmental_tags",
        "description",
    ],
    "additionalProperties": False,
}


def _validate_base_url(url: str) -> None:
    """Absolute https URL, or http only for a loopback host (test servers).

    Rejects credentials, query strings and fragments in the URL: a bearer token
    belongs in the header only. Messages never echo the URL itself.
    """
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or port == -1:
        raise DirectorConfigError("CEREBRAS_API_BASE_URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise DirectorConfigError("CEREBRAS_API_BASE_URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise DirectorConfigError("CEREBRAS_API_BASE_URL must not contain a query or fragment")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise DirectorConfigError(
            "CEREBRAS_API_BASE_URL must use https except for a loopback test server"
        )


def _is_loopback(hostname: str) -> bool:
    if hostname in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class CerebrasConfig:
    """Server-side configuration for the Cerebras provider adapter.

    ``api_key`` is redacted from ``repr`` and never surfaced to clients or logs.
    """

    model: str = DEFAULT_CEREBRAS_MODEL
    api_base_url: str = DEFAULT_API_BASE_URL
    api_key: str = field(default="", repr=False)
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS

    def __post_init__(self) -> None:
        if not _MODEL_ID_RE.match(self.model):
            raise DirectorConfigError("CEREBRAS_MODEL is not a valid model id")
        _validate_base_url(self.api_base_url)
        if self.api_key and not _API_KEY_RE.match(self.api_key):
            raise DirectorConfigError("CEREBRAS_API_KEY contains invalid characters or is too long")
        tokens = self.max_completion_tokens
        if (
            isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or not _MIN_COMPLETION_TOKENS <= tokens <= _MAX_COMPLETION_TOKENS_CEILING
        ):
            raise DirectorConfigError(
                "CEREBRAS_MAX_COMPLETION_TOKENS must be an integer between "
                f"{_MIN_COMPLETION_TOKENS} and {_MAX_COMPLETION_TOKENS_CEILING}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CerebrasConfig:
        env = os.environ if environ is None else environ

        def read(name: str) -> str:
            value = env.get(name)
            return value.strip() if isinstance(value, str) else ""

        raw_tokens = read("CEREBRAS_MAX_COMPLETION_TOKENS")
        try:
            max_tokens = int(raw_tokens) if raw_tokens else DEFAULT_MAX_COMPLETION_TOKENS
        except ValueError:
            raise DirectorConfigError(
                "CEREBRAS_MAX_COMPLETION_TOKENS must be an integer between "
                f"{_MIN_COMPLETION_TOKENS} and {_MAX_COMPLETION_TOKENS_CEILING}"
            ) from None
        return cls(
            model=read("CEREBRAS_MODEL") or DEFAULT_CEREBRAS_MODEL,
            api_base_url=read("CEREBRAS_API_BASE_URL").rstrip("/") or DEFAULT_API_BASE_URL,
            api_key=read("CEREBRAS_API_KEY"),
            max_completion_tokens=max_tokens,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    @property
    def completions_url(self) -> str:
        return f"{self.api_base_url.rstrip('/')}/chat/completions"

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass(frozen=True, slots=True)
class CerebrasTransportRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: JsonValue


@dataclass(frozen=True, slots=True)
class CerebrasTransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class CerebrasTransport(Protocol):
    """One `send` is exactly one HTTP attempt: no retries, ever."""

    async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse: ...


class CerebrasResponseTooLarge(Exception):
    """The upstream body exceeded the adapter's hard cap (never echoed anywhere)."""


class HttpxCerebrasTransport:
    """Production transport over one shared ``httpx.AsyncClient``.

    No client-side timeout: the director's deadline cancels the awaiting task,
    and cancellation closes the streamed response. Redirects are not followed so
    the bearer token can never be replayed to another host.
    """

    def __init__(self, client: Any | None = None) -> None:
        import httpx

        # httpx logs every request URL at INFO; keep that out of server logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._client = client or httpx.AsyncClient(timeout=None, follow_redirects=False)

    async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse:
        body = bytearray()
        async with self._client.stream(
            request.method,
            request.url,
            headers=request.headers,
            json=request.json_body,
        ) as response:
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > _MAX_RESPONSE_BODY_BYTES:
                raise CerebrasResponseTooLarge
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BODY_BYTES:
                    raise CerebrasResponseTooLarge
                body.extend(chunk)
            return CerebrasTransportResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=bytes(body),
            )

    async def aclose(self) -> None:
        await self._client.aclose()


_OPPOSITE = {
    ExitDirection.NORTH: ExitDirection.SOUTH,
    ExitDirection.SOUTH: ExitDirection.NORTH,
    ExitDirection.EAST: ExitDirection.WEST,
    ExitDirection.WEST: ExitDirection.EAST,
    ExitDirection.UP: ExitDirection.DOWN,
    ExitDirection.DOWN: ExitDirection.UP,
}
_VERTICAL = (ExitDirection.UP, ExitDirection.DOWN)


def build_prompt_messages(request: GenerationRequest) -> list[dict[str, str]]:
    """Construct concise chat messages for Cerebras Qwen."""
    state = request.state
    frontier = request.target_exit
    opposite_dir = _OPPOSITE[frontier.direction].value
    back_kind = "stairs" if frontier.direction in _VERTICAL else "door"

    system_prompt = (
        "You are the dungeon director for a roguelike game. Generate a single semantic RoomPlan "
        "for the unexplored exit connecting back to the frontier. Return strictly valid JSON "
        "matching the requested schema."
    )

    context = {
        "task": "generate_room",
        "depth": state.depth,
        "turn": state.turn,
        "player": {
            "hp": state.player.hp,
            "max_hp": state.player.max_hp,
            "level": state.player.level,
            "conditions": state.player.conditions,
        },
        "target_exit_frontier": {
            "from_room": frontier.room_id,
            "exit_direction": frontier.direction.value,
            "required_backlink": {
                "direction": opposite_dir,
                "kind": back_kind,
                "locked": False,
            },
        },
        "options": request.options.model_dump(mode="json") if request.options else None,
        "prompt_hint": request.prompt_hint,
    }

    user_prompt = (
        f"Generate the room behind exit {frontier.direction.value} of room {frontier.room_id} "
        f"at depth {state.depth}. You MUST include an exit with direction='{opposite_dir}', "
        f"kind='{back_kind}', locked=false connecting back to the player.\n"
        f"Context: {json.dumps(context, separators=(',', ':'))}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


class CerebrasProvider(DungeonDirectorProvider):
    """Decides rooms by calling Cerebras Qwen with structured output."""

    def __init__(self, config: CerebrasConfig, transport: CerebrasTransport | None = None) -> None:
        self._config = config
        self._injected_transport = transport
        self._transport: CerebrasTransport | None = transport
        self.provider_id = CEREBRAS_PROVIDER_ID
        self.models = (config.model,)
        self.default_model = config.model

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        transport: CerebrasTransport | None = None,
    ) -> CerebrasProvider:
        return cls(CerebrasConfig.from_env(environ), transport)

    async def aclose(self) -> None:
        if self._transport is not None and self._transport is not self._injected_transport:
            closer = getattr(self._transport, "aclose", None)
            if closer is not None:
                await closer()
        self._transport = self._injected_transport

    @property
    def availability(self) -> ProviderAvailability:
        if self._config.has_credentials:
            return ProviderAvailability(True)
        return ProviderAvailability(
            False,
            "cerebras is registered but not configured: set CEREBRAS_API_KEY to enable it",
        )

    def __repr__(self) -> str:
        configured = self._config.has_credentials
        return f"CerebrasProvider(model={self._config.model!r}, configured={configured})"

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        started = time.perf_counter()
        if not self._config.has_credentials:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, "cerebras has no credentials configured")
        if model not in self.models:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, f"model {model!r} is not configured")

        messages = build_prompt_messages(request)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "room_plan",
                    "strict": True,
                    "schema": _ROOM_PLAN_JSON_SCHEMA,
                },
            },
            "reasoning_effort": "none",
            "max_completion_tokens": self._config.max_completion_tokens,
            "temperature": _TEMPERATURE,
            "seed": _SEED,
        }

        if self._transport is None:
            self._transport = HttpxCerebrasTransport()
        transport = self._transport
        transport_request = CerebrasTransportRequest(
            method="POST",
            url=self._config.completions_url,
            headers={
                **self._config.authorization_header(),
                "Content-Type": "application/json",
            },
            json_body=body,
        )

        try:
            response = await transport.send(transport_request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _transport_error(exc) from exc

        if response.status_code != 200:
            raise _http_status_error(response.status_code)

        content, usage, meta = _parse_chat_completion(_decode_success(response))

        # The raw content goes to the service, which owns RoomPlan validation.
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        meta.update(
            {
                "upstream_http_status": 200,
                "adapter_elapsed_ms": elapsed_ms,
            }
        )

        return ProviderResult(payload=content, usage=usage, provider_metadata=meta)


def _transport_error(exc: Exception) -> ProviderError:
    if isinstance(exc, CerebrasResponseTooLarge):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, "cerebras response exceeded the adapter body limit"
        )
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                ErrorKind.PROVIDER_TIMEOUT, "cerebras request timed out at the transport layer"
            )
        if isinstance(exc, httpx.HTTPError):
            return ProviderError(
                ErrorKind.PROVIDER_ERROR,
                f"cerebras request failed at the transport layer ({type(exc).__name__})",
            )
    except ImportError:
        pass
    return ProviderError(
        ErrorKind.PROVIDER_ERROR,
        f"cerebras transport raised {type(exc).__name__}",
    )


def _http_status_error(status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR,
            f"cerebras rejected the credentials (HTTP {status})",
        )
    if status == 429:
        return ProviderError(ErrorKind.RATE_LIMITED, "cerebras rate limit reached (HTTP 429)")
    if status == 404:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, "cerebras reported HTTP 404 (endpoint or model not found)"
        )
    if status in (400, 422):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"cerebras rejected the request body (HTTP {status})"
        )
    if status >= 500:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"cerebras reported an upstream error (HTTP {status})"
        )
    return ProviderError(ErrorKind.PROVIDER_ERROR, f"cerebras returned HTTP {status}")


def _decode_success(response: CerebrasTransportResponse) -> dict[str, Any]:
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            ErrorKind.INVALID_JSON, "cerebras response body was not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "cerebras response was not a JSON object")
    return body


def _parse_chat_completion(
    payload: dict[str, Any],
) -> tuple[str, UsageStats | None, dict[str, JsonValue]]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError(ErrorKind.EMPTY_RESPONSE, "cerebras response has no choices")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "cerebras choice was not a dict")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "cerebras choice has no message")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError(ErrorKind.EMPTY_RESPONSE, "cerebras message has empty content")

    finish_reason = first_choice.get("finish_reason")

    usage = None
    raw_usage = payload.get("usage")
    usage_prompt = None
    usage_completion = None
    if isinstance(raw_usage, dict):
        usage_prompt = _usage_int(raw_usage.get("prompt_tokens"))
        usage_completion = _usage_int(raw_usage.get("completion_tokens"))
        usage = UsageStats(
            input_tokens=usage_prompt,
            output_tokens=usage_completion,
        )

    meta: dict[str, JsonValue] = {}
    cerebras_id = payload.get("id")
    if isinstance(cerebras_id, str):
        meta["cerebras_id"] = cerebras_id[:64]
    cerebras_model = payload.get("model")
    if isinstance(cerebras_model, str):
        meta["cerebras_model"] = cerebras_model[:64]
    if isinstance(finish_reason, str):
        meta["finish_reason"] = finish_reason[:32]
    if usage_prompt is not None:
        meta["prompt_tokens"] = usage_prompt
    if usage_completion is not None:
        meta["completion_tokens"] = usage_completion

    time_info = payload.get("time_info")
    if isinstance(time_info, dict):
        for key, value in time_info.items():
            if len(meta) >= _META_MAX_KEYS:
                break
            if (
                isinstance(key, str)
                and _META_KEY_RE.match(key)
                and isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                meta[f"time_{key}"] = round(float(value), 4)

    return content, usage, meta


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return min(max(value, 0), 10_000_000)
