"""Groq provider adapter for GPT-OSS with strict structured output (issue #9).

A fast *generative* model, kept next to Jev for comparison: one chat
completion asks the model for a complete :class:`RoomPlan` as constrained JSON.
Everything the game needs is decided by the model; geometry stays game-side.

Verified API contract (sources in ``director/docs/groq.md``):

* ``POST {GROQ_API_BASE_URL}/chat/completions`` (default base URL
  ``https://api.groq.com/openai/v1``) with ``Authorization: Bearer <key>``.
* ``response_format = {"type": "json_schema", "json_schema": {"name": ...,
  "strict": true, "schema": ...}}`` enables constrained decoding on
  ``openai/gpt-oss-20b`` (and ``-120b``). Strict mode needs every property in
  ``required``, ``additionalProperties: false`` on every object, optional
  values as ``["type", "null"]`` unions, and no range/length/pattern keywords:
  those bounds are enforced afterwards by the canonical contract.
* GPT-OSS models accept ``reasoning_effort`` of ``low``, ``medium`` or
  ``high`` only (there is no ``none``/``minimal``), and ``include_reasoning:
  false`` drops the reasoning text from the response. We send ``low`` and
  ``false``; ``reasoning_format`` is not supported on GPT-OSS and is mutually
  exclusive with ``include_reasoning``, so it is never sent.
* Responses carry ``usage`` with token counts and ``queue_time`` /
  ``prompt_time`` / ``completion_time`` / ``total_time`` in seconds, plus
  ``x_groq.id`` as the request id.

Adapter rules (matching the service's trust model, identical to Jev):

* The API key lives only in :class:`GroqConfig`, is read from the environment
  once at construction, and never appears in ``repr``, the registry,
  ``/v1/config``, responses, logs, errors or test output.
* Exactly one HTTP call per ``generate``: no retries, no adapter-side
  timeouts, no background work. The service owns the deadline and cancels
  this task on timeout; ``CancelledError`` always propagates.
* Failures are classified :class:`~dungeon_director.errors.ProviderError`
  values whose text carries no upstream body and no credentials.
* The model's JSON is returned **verbatim** as the payload. The service alone
  validates it against ``RoomPlan``; malformed or out-of-contract output
  becomes a recorded failure (with the excerpt, token usage and timing
  metadata), never a silent repair.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
    "DEFAULT_GROQ_MODEL",
    "GROQ_PROVIDER_ID",
    "ROOM_PLAN_SCHEMA",
    "GroqConfig",
    "GroqProvider",
    "GroqTransport",
    "GroqTransportRequest",
    "GroqTransportResponse",
    "HttpxGroqTransport",
    "build_request_body",
]

GROQ_PROVIDER_ID = "groq"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_API_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_REASONING_EFFORT = "low"
DEFAULT_MAX_COMPLETION_TOKENS = 2048

#: Names only: they appear in operator-facing availability reasons.
REQUIRED_ENV_VARS = ("GROQ_API_KEY",)

#: ``reasoning_effort`` values in Groq's API reference. GPT-OSS models accept
#: only the first three (Groq's reasoning guide); ``none`` belongs to Qwen.
_GPT_OSS_EFFORTS = ("low", "medium", "high")
_API_EFFORTS = ("none", "default", "minimal", *_GPT_OSS_EFFORTS, "xhigh", "max")
_GPT_OSS_PREFIX = "openai/gpt-oss"

# Every pattern below is applied with ``fullmatch``: ``$`` would also match
# before a final "\n", which would smuggle a newline into a header or URL.
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}")
_KEY_RE = re.compile(r"[\x21-\x7e]{1,4096}")  # printable ASCII: safe as a header value
_URL_RE = re.compile(r"[\x21-\x7e]{1,2048}")  # no whitespace or control characters
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,31}")
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_MAX_RESPONSE_BODY_BYTES = 1_048_576
_MIN_COMPLETION_TOKENS = 64
_MAX_COMPLETION_TOKENS = 65_536  # openai/gpt-oss-20b's documented ceiling
_MAX_USAGE_COUNT = 10_000_000
_MAX_SECONDS = 86_400.0


def _is_gpt_oss(model: str) -> bool:
    return model.startswith(_GPT_OSS_PREFIX)


# ---------------------------------------------------------------------------
# Configuration (the key never leaves this object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroqConfig:
    """Server-side configuration for the Groq adapter.

    Validation errors name the environment variable but never echo values.
    ``api_key`` is excluded from ``repr`` and must be header-safe (printable
    ASCII), so it cannot inject headers.
    """

    model: str = DEFAULT_GROQ_MODEL
    api_base_url: str = DEFAULT_API_BASE_URL
    api_key: str = field(default="", repr=False)
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS

    def __post_init__(self) -> None:
        if self.api_key and not _KEY_RE.fullmatch(self.api_key):
            raise DirectorConfigError(
                "GROQ_API_KEY must be 1-4096 printable ASCII characters without spaces"
            )
        if not _MODEL_ID_RE.fullmatch(self.model):
            raise DirectorConfigError(
                "GROQ_MODEL must be a model id such as 'openai/gpt-oss-20b' "
                "(letters, digits, '_', '.', ':', '/', '@', '-'; at most 128 chars)"
            )
        if not isinstance(self.api_base_url, str) or not _URL_RE.fullmatch(self.api_base_url):
            raise DirectorConfigError(
                "GROQ_API_BASE_URL must not contain whitespace or control characters"
            )
        try:
            parsed = urlsplit(self.api_base_url)
            _validated_port = parsed.port
        except ValueError:
            raise DirectorConfigError("GROQ_API_BASE_URL must be an absolute http(s) URL") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DirectorConfigError("GROQ_API_BASE_URL must be an absolute http(s) URL")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise DirectorConfigError(
                "GROQ_API_BASE_URL must not contain credentials, a query or a fragment"
            )
        if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
            raise DirectorConfigError(
                "GROQ_API_BASE_URL must use https except for a loopback test server"
            )
        allowed = _GPT_OSS_EFFORTS if _is_gpt_oss(self.model) else _API_EFFORTS
        if self.reasoning_effort not in allowed:
            raise DirectorConfigError(
                f"GROQ_REASONING_EFFORT must be one of {', '.join(allowed)} for model family "
                f"{'GPT-OSS' if _is_gpt_oss(self.model) else 'non-GPT-OSS'}"
            )
        tokens = self.max_completion_tokens
        if (
            isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or not _MIN_COMPLETION_TOKENS <= tokens <= _MAX_COMPLETION_TOKENS
        ):
            raise DirectorConfigError(
                f"GROQ_MAX_COMPLETION_TOKENS must be an integer from {_MIN_COMPLETION_TOKENS} "
                f"to {_MAX_COMPLETION_TOKENS}"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> GroqConfig:
        env = os.environ if environ is None else environ

        def read(name: str) -> str:
            value = env.get(name)
            return value.strip() if isinstance(value, str) else ""

        raw_tokens = read("GROQ_MAX_COMPLETION_TOKENS")
        try:
            tokens = int(raw_tokens) if raw_tokens else DEFAULT_MAX_COMPLETION_TOKENS
        except ValueError:
            raise DirectorConfigError("GROQ_MAX_COMPLETION_TOKENS must be an integer") from None
        return cls(
            model=read("GROQ_MODEL") or DEFAULT_GROQ_MODEL,
            api_base_url=read("GROQ_API_BASE_URL").rstrip("/") or DEFAULT_API_BASE_URL,
            api_key=read("GROQ_API_KEY"),
            reasoning_effort=read("GROQ_REASONING_EFFORT") or DEFAULT_REASONING_EFFORT,
            max_completion_tokens=tokens,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key)

    @property
    def chat_completions_url(self) -> str:
        return f"{self.api_base_url.rstrip('/')}/chat/completions"

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


# ---------------------------------------------------------------------------
# Injected async transport (deterministic tests; no hidden retries)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroqTransportRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: JsonValue


@dataclass(frozen=True, slots=True)
class GroqTransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class GroqTransport(Protocol):
    """One ``send`` is exactly one HTTP attempt: no retries, ever."""

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse: ...


class HttpxGroqTransport:
    """Production transport over one shared ``httpx.AsyncClient``.

    The client has no timeout of its own: the director's deadline cancels the
    awaiting task, which aborts the in-flight request. Redirects are never
    followed, so the bearer token cannot be replayed to another host.
    """

    def __init__(self, client: Any | None = None) -> None:
        import httpx  # lazy: unit tests never need a real client

        self._client = client or httpx.AsyncClient(timeout=None, follow_redirects=False)

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse:
        body = bytearray()
        async with self._client.stream(
            request.method,
            request.url,
            headers=request.headers,
            json=request.json_body,
        ) as response:
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BODY_BYTES:
                    raise RuntimeError("groq response exceeded the adapter body limit")
                body.extend(chunk)
            return GroqTransportResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=bytes(body),
            )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Strict structured-output schema (mirrors RoomPlan; bounds live in the contract)
# ---------------------------------------------------------------------------


def _enum(values: Any) -> dict[str, JsonValue]:
    return {"type": "string", "enum": [member.value for member in values]}


#: Strict-mode JSON Schema for one :class:`RoomPlan`. Constrained decoding
#: guarantees shape and enums; numeric ranges, list sizes and the semantic
#: invariants are deliberately left to ``RoomPlan`` validation in the service,
#: because strict mode does not support those keywords.
ROOM_PLAN_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "room_id": {"type": "string"},
        "depth": {"type": "integer"},
        "room_type": _enum(RoomType),
        "size": _enum(RoomSize),
        "danger": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "exits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "direction": _enum(ExitDirection),
                    "kind": _enum(ExitKind),
                    "locked": {"type": "boolean"},
                },
                "required": ["direction", "kind", "locked"],
            },
        },
        "enemy_density": {"type": "number"},
        "loot_density": {"type": "number"},
        "secret_probability": {"type": "number"},
        "has_secret": {"type": ["boolean", "null"]},
        "environmental_tags": {"type": "array", "items": _enum(EnvironmentalTag)},
        "description": {"type": ["string", "null"]},
    },
    "required": [
        "room_id",
        "depth",
        "room_type",
        "size",
        "danger",
        "exits",
        "enemy_density",
        "loot_density",
        "secret_probability",
        "has_secret",
        "environmental_tags",
        "description",
    ],
}

_SCHEMA_NAME = "room_plan"

# ---------------------------------------------------------------------------
# Compact, deterministic prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are the dungeon director of a turn-based roguelike. Plan ONE new room: "
    "semantics only, never tile geometry. Reply with a single JSON object matching "
    "the schema and nothing else.\n"
    "Rules:\n"
    "- room_id and depth: copy `room_id` and `depth` from the input.\n"
    "- exits: exactly one exit facing `back_direction` with kind `back_kind` and "
    "locked false, plus 0-3 more exits with distinct directions.\n"
    "- danger is 1-5: lower it when player hp is low relative to max_hp; never "
    "above options.max_danger.\n"
    "- enemy_density, loot_density and secret_probability are numbers from 0 to 1; "
    "use options.target_enemy_density and options.target_loot_density when given.\n"
    "- has_secret true needs secret_probability above 0; options.allow_secrets "
    "false means has_secret false and secret_probability 0.\n"
    "- room_type: never entrance or stairs_up; never one of options."
    "forbidden_room_types; stairs_down only when pacing.rooms_on_depth is at "
    "least 6; vault and treasure only from depth 2.\n"
    "- environmental_tags: at most 3; never icy together with hot.\n"
    "- description: one short sentence, or null.\n"
    "Vary from recent_rooms and respect the hint."
)

_OPPOSITE = {
    ExitDirection.NORTH: ExitDirection.SOUTH,
    ExitDirection.SOUTH: ExitDirection.NORTH,
    ExitDirection.EAST: ExitDirection.WEST,
    ExitDirection.WEST: ExitDirection.EAST,
    ExitDirection.UP: ExitDirection.DOWN,
    ExitDirection.DOWN: ExitDirection.UP,
}
_VERTICAL = (ExitDirection.UP, ExitDirection.DOWN)

#: Compactness bounds for the state sent upstream (the contract allows more;
#: tokens cost money and latency).
_MAX_ROOMS_IN_PROMPT = 6
_MAX_EVENTS_IN_PROMPT = 5
_MAX_ITEMS_IN_PROMPT = 12


def _request_digest(request: GenerationRequest, model: str) -> str:
    return hashlib.sha256(f"{request.run_id}:{request.request_id}:{model}".encode()).hexdigest()


def _build_state(request: GenerationRequest, room_id: str) -> dict[str, JsonValue]:
    state = request.state
    player = state.player
    frontier = request.target_exit.direction
    prompt: dict[str, JsonValue] = {
        "room_id": room_id,
        "depth": state.depth,
        "turn": state.turn,
        "frontier": {"room_id": request.target_exit.room_id, "direction": frontier.value},
        "back_direction": _OPPOSITE[frontier].value,
        "back_kind": (ExitKind.STAIRS if frontier in _VERTICAL else ExitKind.DOOR).value,
        "player": {
            "hp": player.hp,
            "max_hp": player.max_hp,
            "level": player.level,
            "hunger": player.hunger.value if player.hunger else None,
            "conditions": list(player.conditions),
        },
        "recent_rooms": [
            {"type": room.room_type.value, "danger": room.danger}
            for room in state.recent_rooms[-_MAX_ROOMS_IN_PROMPT:]
        ],
        "recent_events": [event.event for event in state.recent_events[-_MAX_EVENTS_IN_PROMPT:]],
        "inventory": [
            {"id": item.item_id, "qty": item.quantity}
            for item in state.inventory[:_MAX_ITEMS_IN_PROMPT]
        ],
    }
    if state.pacing is not None:
        prompt["pacing"] = state.pacing.model_dump(mode="json", exclude_none=True)
    if request.options is not None:
        prompt["options"] = request.options.model_dump(mode="json", exclude_none=True)
    if request.prompt_hint:
        prompt["hint"] = request.prompt_hint
    return prompt


def build_request_body(
    request: GenerationRequest, config: GroqConfig, model: str
) -> dict[str, JsonValue]:
    """The exact chat-completions body for ``request``: deterministic per request.

    ``temperature`` 0 plus a seed derived from the request identity gives
    best-effort reproducibility (Groq documents ``seed`` as best effort).
    """
    digest = _request_digest(request, model)
    user_state = _build_state(request, f"groq-{digest[:10]}")
    body: dict[str, JsonValue] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(user_state, separators=(",", ":"), sort_keys=True),
            },
        ],
        "temperature": 0,
        "seed": int(digest[:8], 16) % 2**31,
        "max_completion_tokens": config.max_completion_tokens,
        "stream": False,
        "reasoning_effort": config.reasoning_effort,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": _SCHEMA_NAME, "strict": True, "schema": ROOM_PLAN_SCHEMA},
        },
    }
    if _is_gpt_oss(model):
        body["include_reasoning"] = False  # reasoning text is never returned or stored
    return body


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class GroqProvider(DungeonDirectorProvider):
    """Asks Groq's GPT-OSS for a strict-schema :class:`RoomPlan`.

    One ``generate`` is one chat completion. The model's JSON text is handed
    back unmodified; the service validates it against the contract.
    """

    def __init__(self, config: GroqConfig, transport: GroqTransport | None = None) -> None:
        self._config = config
        self._injected_transport = transport
        self._transport: GroqTransport | None = transport
        self.provider_id = GROQ_PROVIDER_ID
        self.models = (config.model,)
        self.default_model = config.model

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        transport: GroqTransport | None = None,
    ) -> GroqProvider:
        return cls(GroqConfig.from_env(environ), transport)

    async def aclose(self) -> None:
        """Close the HTTP client if the provider owns it; injected ones stay untouched."""
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
            f"groq is registered but not configured: set {' and '.join(REQUIRED_ENV_VARS)} "
            "to enable it",
        )

    def __repr__(self) -> str:  # the key never reaches repr
        return (
            f"GroqProvider(model={self._config.model!r}, configured={self._config.has_credentials})"
        )

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        started = time.perf_counter()
        if not self._config.has_credentials:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, "groq has no API key configured")
        if model not in self.models:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, f"model {model!r} is not configured")

        body = build_request_body(request, self._config, model)
        transport_request = GroqTransportRequest(
            method="POST",
            url=self._config.chat_completions_url,
            headers={**self._config.authorization_header(), "Content-Type": "application/json"},
            json_body=body,
        )
        # One shared lazily-created transport (and HTTP client) per provider.
        if self._transport is None:
            self._transport = HttpxGroqTransport()
        transport = self._transport

        try:
            response = await transport.send(transport_request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _transport_error(exc) from exc

        if response.status_code != 200:
            raise _http_status_error(response.status_code)

        decoded = _decode_body(response)
        content, choice_metadata = _first_choice(decoded)
        usage, metadata = _telemetry(decoded)
        metadata.update(choice_metadata)
        metadata.update(
            {
                "reasoning_effort": self._config.reasoning_effort,
                "max_completion_tokens": self._config.max_completion_tokens,
                "seed": body["seed"],
                "strict_schema": True,
                "upstream_http_status": 200,
                "adapter_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        return ProviderResult(payload=content, usage=usage, provider_metadata=metadata)


# ---------------------------------------------------------------------------
# Upstream response handling
# ---------------------------------------------------------------------------


def _transport_error(exc: Exception) -> ProviderError:
    # The message carries the exception type only: text and tracebacks can echo
    # the request target or headers.
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                ErrorKind.PROVIDER_TIMEOUT, "groq request timed out at the transport layer"
            )
        if isinstance(exc, httpx.HTTPError):
            return ProviderError(
                ErrorKind.PROVIDER_ERROR,
                f"groq request failed at the transport layer ({type(exc).__name__})",
            )
    except ImportError:  # pragma: no cover - httpx is a runtime dependency
        pass
    return ProviderError(ErrorKind.PROVIDER_ERROR, f"groq transport raised {type(exc).__name__}")


def _http_status_error(status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"groq rejected the credentials (HTTP {status})"
        )
    if status in (429, 498):  # 498: Groq flex-tier capacity exceeded
        return ProviderError(ErrorKind.RATE_LIMITED, f"groq rate limit or capacity (HTTP {status})")
    if status in (400, 413, 422):
        return ProviderError(ErrorKind.PROVIDER_ERROR, f"groq rejected the request (HTTP {status})")
    if status >= 500:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"groq reported an upstream error (HTTP {status})"
        )
    return ProviderError(ErrorKind.PROVIDER_ERROR, f"groq returned HTTP {status}")


def _decode_body(response: GroqTransportResponse) -> Mapping[str, Any]:
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ProviderError(
            ErrorKind.INVALID_JSON, "groq response body was not valid JSON"
        ) from exc
    if not isinstance(body, Mapping):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "groq response was not a JSON object")
    if "error" in body:
        raise ProviderError(ErrorKind.PROVIDER_ERROR, "groq returned an error object with HTTP 200")
    return body


def _first_choice(body: Mapping[str, Any]) -> tuple[str, dict[str, JsonValue]]:
    """The first choice's raw content text plus its bounded metadata.

    Missing or ``null`` content is returned as ``""`` so that the service
    records an ``empty_response`` failure *with* usage and timing metadata
    (typical when reasoning exhausted ``max_completion_tokens``).
    """
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "groq response has no usable choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "groq choice has no message object")
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise ProviderError(ErrorKind.SAFETY_REFUSAL, "groq refused the request")
    content = message.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "groq message content was not text")
    metadata: dict[str, JsonValue] = {"response_chars": len(content)}
    finish_reason = _label(choice.get("finish_reason"), _LABEL_RE)
    if finish_reason is not None:
        metadata["finish_reason"] = finish_reason
    return content, metadata


def _telemetry(body: Mapping[str, Any]) -> tuple[UsageStats | None, dict[str, JsonValue]]:
    """Token usage and bounded provider timing/identity fields (all optional)."""
    metadata: dict[str, JsonValue] = {}
    for name, pattern in (
        ("groq_model", _MODEL_ID_RE),
        ("service_tier", _LABEL_RE),
        ("system_fingerprint", _REQUEST_ID_RE),
    ):
        label = _label(body.get(name.removeprefix("groq_")), pattern)
        if label is not None:
            metadata[name] = label
    x_groq = body.get("x_groq")
    if isinstance(x_groq, Mapping):
        request_id = _label(x_groq.get("id"), _REQUEST_ID_RE)
        if request_id is not None:
            metadata["groq_request_id"] = request_id

    raw_usage = body.get("usage")
    if not isinstance(raw_usage, Mapping):
        return None, metadata
    counts = {
        "prompt_tokens": _count(raw_usage.get("prompt_tokens")),
        "completion_tokens": _count(raw_usage.get("completion_tokens")),
        "total_tokens": _count(raw_usage.get("total_tokens")),
    }
    details = raw_usage.get("completion_tokens_details")
    if isinstance(details, Mapping):
        counts["reasoning_tokens"] = _count(details.get("reasoning_tokens"))
    seconds = {
        "queue_time_s": _seconds(raw_usage.get("queue_time")),
        "prompt_time_s": _seconds(raw_usage.get("prompt_time")),
        "completion_time_s": _seconds(raw_usage.get("completion_time")),
        "total_time_s": _seconds(raw_usage.get("total_time")),
    }
    for name, value in {**counts, **seconds}.items():
        if value is not None:
            metadata[name] = value
    usage = UsageStats(
        input_tokens=counts["prompt_tokens"], output_tokens=counts["completion_tokens"]
    )
    return usage, metadata


def _label(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _MAX_USAGE_COUNT else None


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= _MAX_SECONDS:
        return None
    return round(number, 6)
