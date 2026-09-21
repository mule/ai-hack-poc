"""Cloudflare TypeSafe Jev provider adapter (issue #8).

Jev is TypeSafe's structured evaluation model ("System One"): it does not
generate prose. One call sends a ``state`` plus a map of typed questions
(``noul`` boolean-probability, ``choice`` single pick, ``score`` rubric
position) and returns one typed, calibrated answer per question. That maps
exactly onto the director's job: decide the *semantics* of the next room;
code composes the answers into a canonical :class:`RoomPlan` and derives all
geometry elsewhere.

Verified API contract (sources in ``director/docs/cloudflare-jev.md``):

* ``POST {CLOUDFLARE_JEV_API_BASE_URL}/accounts/{account_id}/ai/run``
  with ``Authorization: Bearer <token>`` and body
  ``{"model": "<model id>", "input": {"state": ..., "questions": {...}}}``.
* The default model id is ``typesafe/jev`` (Cloudflare unified AI catalog,
  third-party; it is *not* a ``@cf/`` Workers-AI-hosted model).
* Answers arrive as JSON, either bare (``{"model", "answers", "usage"}``, as
  shown on the model's catalog page) or inside the standard Cloudflare v4
  envelope ``{"result": ..., "success": true, "errors": [], "messages": []}``
  documented for the REST API; both shapes are accepted.

Adapter rules (matching the service's trust model):

* Credentials live only in :class:`JevConfig`, are read from the environment
  once at construction, and never appear in ``repr``, the registry,
  ``/v1/config``, responses, logs, errors or test output.
* Exactly one HTTP call per ``generate``: no retries, no adapter-side
  timeouts, no background work. The service owns the deadline and cancels
  this task on timeout; every ``await`` here is cancellable, and
  ``CancelledError`` always propagates.
* Failures are raised as classified :class:`~dungeon_director.errors.ProviderError`
  with messages that carry no upstream body text and no credentials.
* Only bounded, JSON-safe decision telemetry is returned in
  ``provider_metadata``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Container, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import JsonValue

from dungeon_director.contracts import (
    EnvironmentalTag,
    Exit,
    ExitDirection,
    ExitKind,
    GenerationRequest,
    RoomPlan,
    RoomSize,
    RoomType,
    UsageStats,
)
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.providers import DungeonDirectorProvider, ProviderAvailability, ProviderResult

__all__ = [
    "CLOUDFLARE_JEV_PROVIDER_ID",
    "DEFAULT_JEV_MODEL",
    "HttpxJevTransport",
    "JevConfig",
    "JevTransport",
    "JevTransportRequest",
    "JevTransportResponse",
    "CloudflareJevProvider",
    "compose_jev_room",
    "decode_jev_payload",
]

CLOUDFLARE_JEV_PROVIDER_ID = "cloudflare-jev"
DEFAULT_JEV_MODEL = "typesafe/jev"
DEFAULT_API_BASE_URL = "https://api.cloudflare.com/client/v4"

#: Environment variables that must be non-blank for the provider to be usable.
#: Names only: they appear in operator-facing availability reasons.
REQUIRED_ENV_VARS = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN")

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,128}$")
_URL_RE = re.compile(r"[\x21-\x7e]{1,2048}")  # printable ASCII, no whitespace/control chars
_TOKEN_MAX_CHARS = 4096
_MAX_RESPONSE_BODY_BYTES = 1_048_576
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

#: Numeric tolerance for float noise at rubric boundaries. Values further out
#: than this are hostile output and become schema violations, never clamps.
_EPSILON = 1e-9


# ---------------------------------------------------------------------------
# Configuration (credentials never leave this object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JevConfig:
    """Server-side configuration for the Cloudflare Jev adapter.

    ``account_id`` and ``api_token`` are credentials-in-context: they are
    excluded from ``repr`` (an account id is half of a URL an attacker could
    probe) and are never surfaced by the provider, registry or API. The
    account id is charset-validated before it can reach a URL: interpolation
    of unvalidated values would allow path/scheme injection.
    """

    model: str = DEFAULT_JEV_MODEL
    api_base_url: str = DEFAULT_API_BASE_URL
    account_id: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.account_id and not _ACCOUNT_ID_RE.fullmatch(self.account_id):
            raise DirectorConfigError(
                "CLOUDFLARE_ACCOUNT_ID must be alphanumeric with '_' or '-' "
                f"(1-129 chars, starting alphanumeric); got {len(self.account_id)} invalid chars"
            )
        if not isinstance(self.api_base_url, str) or not _URL_RE.fullmatch(self.api_base_url):
            raise DirectorConfigError(
                "CLOUDFLARE_JEV_API_BASE_URL must not contain whitespace or control characters"
            )
        try:
            parsed_url = urlsplit(self.api_base_url)
            _validated_port = parsed_url.port
        except ValueError:
            raise DirectorConfigError(
                "CLOUDFLARE_JEV_API_BASE_URL must be an absolute http(s) URL"
            ) from None
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise DirectorConfigError("CLOUDFLARE_JEV_API_BASE_URL must be an absolute http(s) URL")
        if (
            parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise DirectorConfigError(
                "CLOUDFLARE_JEV_API_BASE_URL must not contain credentials, a query or a fragment"
            )
        if parsed_url.scheme == "http" and parsed_url.hostname not in _LOOPBACK_HOSTS:
            raise DirectorConfigError(
                "CLOUDFLARE_JEV_API_BASE_URL must use https except for a loopback test server"
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JevConfig:
        env = os.environ if environ is None else environ

        def read(name: str) -> str:
            value = env.get(name)
            return value.strip() if isinstance(value, str) else ""

        account_id = read("CLOUDFLARE_ACCOUNT_ID")
        token = read("CLOUDFLARE_API_TOKEN")[:_TOKEN_MAX_CHARS]
        model = read("CLOUDFLARE_JEV_MODEL") or DEFAULT_JEV_MODEL
        api_base_url = read("CLOUDFLARE_JEV_API_BASE_URL").rstrip("/") or DEFAULT_API_BASE_URL
        return cls(
            model=model,
            api_base_url=api_base_url,
            account_id=account_id,
            api_token=token,
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.account_id and self.api_token)

    @property
    def run_url(self) -> str:
        return f"{self.api_base_url}/accounts/{self.account_id}/ai/run"

    def authorization_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}


# ---------------------------------------------------------------------------
# Injected async transport (deterministic tests; no hidden retries)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JevTransportRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: JsonValue


@dataclass(frozen=True, slots=True)
class JevTransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class JevTransport(Protocol):
    """One ``send`` is exactly one HTTP attempt: no retries, ever."""

    async def send(self, request: JevTransportRequest) -> JevTransportResponse: ...


class HttpxJevTransport:
    """Production transport over one shared ``httpx.AsyncClient``.

    The client has no timeout of its own: the director's deadline cancels the
    awaiting task, which aborts the in-flight request. httpx raises
    ``httpx.TimeoutException`` only if sockets time out at the OS level
    (e.g. keepalive); the adapter maps that to a provider-side timeout.
    """

    def __init__(self, client: Any | None = None) -> None:
        # Imported lazily so unit tests never need a running event loop client.
        import httpx

        # httpx's INFO request log contains the complete URL, including the
        # Cloudflare account id. This adapter treats that id as sensitive.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._client = client or httpx.AsyncClient(timeout=None, follow_redirects=False)

    async def send(self, request: JevTransportRequest) -> JevTransportResponse:
        body = bytearray()
        async with self._client.stream(
            request.method,
            request.url,
            headers=request.headers,
            json=request.json_body,
        ) as response:
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BODY_BYTES:
                    raise RuntimeError("jev response exceeded the adapter body limit")
                body.extend(chunk)
            return JevTransportResponse(
                status_code=response.status_code,
                headers=response.headers,
                body=bytes(body),
            )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Question rubric: the fixed decision set sent for every room
# ---------------------------------------------------------------------------

_ROOM_TYPE_CRITERIA: dict[RoomType, str] = {
    RoomType.ROOM: "A standard dungeon room with mixed contents.",
    RoomType.CORRIDOR: "A narrow passage that carries the dungeon onward.",
    RoomType.CAVERN: "A large, irregular natural cavity.",
    RoomType.CHAMBER: "A worked-stone chamber built for a purpose.",
    RoomType.VAULT: "A fortified room guarding valuables; deeper floors only.",
    RoomType.SHRINE: "A small consecrated place of quiet and recovery.",
    RoomType.SHOP: "A keeper's outpost selling supplies.",
    RoomType.TREASURE: "A rich trove room; deeper floors only.",
    RoomType.STAIRS_DOWN: (
        "A way down to the next depth; fits once several rooms of this depth are known."
    ),
}
_SIZE_CRITERIA: dict[RoomSize, str] = {
    RoomSize.TINY: "A cramped closet of a space.",
    RoomSize.SMALL: "A modest space, quick to explore.",
    RoomSize.MEDIUM: "An ordinarily sized space.",
    RoomSize.LARGE: "A spacious hall.",
    RoomSize.HUGE: "A vast, sweeping space.",
}
_DANGER_LEVELS = (
    "Danger 1: trivial, nothing threatening.",
    "Danger 2: light, weak monsters or minor hazards.",
    "Danger 3: moderate, a fair fight.",
    "Danger 4: hard, dangerous for a wounded adventurer.",
    "Danger 5: deadly, likely to kill the unprepared.",
)
_ENEMY_LEVELS = (
    "Empty: no enemies at all.",
    "Sparse: an isolated threat or two.",
    "Steady: a normal complement of enemies.",
    "Heavy: frequent enemies.",
    "Teeming: enemies at every turn.",
)
_LOOT_LEVELS = (
    "Barren: nothing to find.",
    "Scant: incidental sundries.",
    "Steady: a normal amount of loot.",
    "Rich: worthwhile finds.",
    "Treasure trove: exceptional loot.",
)
_EXIT_COUNT_CRITERIA = {
    "0": "A dead end.",
    "1": "One new branch.",
    "2": "Two new branches.",
    "3": "A junction with three new branches.",
}
_TAG_STATEMENTS: dict[EnvironmentalTag, str] = {
    EnvironmentalTag.DARK: "This room is dark, hindering sight.",
    EnvironmentalTag.FLOODED: "This room is partly flooded with water.",
    EnvironmentalTag.FUNGAL: "Fungal growth spreads across this room.",
    EnvironmentalTag.ICY: "This room is freezing and slick with ice.",
    EnvironmentalTag.HOT: "This room is hot: steam, embers, or geothermal heat.",
    EnvironmentalTag.RUINED: "This room is collapsed or ruined masonry.",
    EnvironmentalTag.HALLOWED: "This room is consecrated and calm.",
    EnvironmentalTag.TRAPPED: "This room conceals mechanical or magical traps.",
    EnvironmentalTag.OVERGROWN: "Roots and strange plants overgrow this room.",
    EnvironmentalTag.NOISY: "This room drones with unsettling noise.",
}
_ATMOSPHERE_CRITERIA = {
    "none": "No single environmental motif should dominate this room.",
    **{tag.value: statement for tag, statement in _TAG_STATEMENTS.items()},
}

#: Pacing gates for which room types are even offered (mirrors the rules
#: baseline so pacing stays code-owned and comparable across providers).
_ROOMS_BEFORE_STAIRS = 6
_TREASURE_MIN_DEPTH = 2

#: A noul at or above this probability counts as "true" (also keeps the
#: RoomPlan invariant: has_secret=true implies secret_probability > 0).
_NOUL_TRUE_THRESHOLD = 0.5

_OPPOSITE = {
    ExitDirection.NORTH: ExitDirection.SOUTH,
    ExitDirection.SOUTH: ExitDirection.NORTH,
    ExitDirection.EAST: ExitDirection.WEST,
    ExitDirection.WEST: ExitDirection.EAST,
    ExitDirection.UP: ExitDirection.DOWN,
    ExitDirection.DOWN: ExitDirection.UP,
}
_CARDINALS = (ExitDirection.NORTH, ExitDirection.SOUTH, ExitDirection.EAST, ExitDirection.WEST)
_VERTICAL = (ExitDirection.UP, ExitDirection.DOWN)

#: Compactness bounds for the state sent upstream (the contract allows more;
#: tokens cost money and latency).
_MAX_ROOMS_IN_STATE = 8
_MAX_EVENTS_IN_STATE = 8
_MAX_INVENTORY_IN_STATE = 24


def _eligible_room_types(request: GenerationRequest) -> list[RoomType]:
    """Room types offered to Jev after code-owned pacing and option gates."""
    forbidden = set(request.options.forbidden_room_types) if request.options else set()
    rooms_on_depth = request.state.pacing.rooms_on_depth if request.state.pacing else 0
    depth = request.state.depth
    eligible = [room_type for room_type in _ROOM_TYPE_CRITERIA if room_type not in forbidden]
    preferred = []
    for room_type in eligible:
        if room_type is RoomType.STAIRS_DOWN and rooms_on_depth < _ROOMS_BEFORE_STAIRS:
            continue
        if room_type in (RoomType.VAULT, RoomType.TREASURE) and depth < _TREASURE_MIN_DEPTH:
            continue
        preferred.append(room_type)
    # Pacing gates are preferences. The contract permits forbidding eight of
    # the nine room types, so relaxing them is the only way to honour the
    # caller's hard forbidden list when its sole remaining type is gated.
    return preferred or eligible


def build_jev_state(request: GenerationRequest) -> dict[str, JsonValue]:
    """Compact, JSON-safe Jev state describing the dungeon decision.

    Includes only committed, player-visible facts (the contract's own bounds
    are larger; these slices keep the state small and the latency honest).
    """
    state = request.state
    player = state.player
    pacing = state.pacing.model_dump(mode="json") if state.pacing else None
    return {
        "game": "roguelike dungeon director",
        "task": "decide the room behind the unexplored exit",
        "depth": state.depth,
        "turn": state.turn,
        "player": {
            "hp": player.hp,
            "max_hp": player.max_hp,
            "hp_ratio": round(player.hp / player.max_hp, 3),
            "level": player.level,
            "hunger": player.hunger.value if player.hunger else None,
            "conditions": [c for c in player.conditions],
        },
        "recent_rooms": [
            room.model_dump(mode="json") for room in state.recent_rooms[-_MAX_ROOMS_IN_STATE:]
        ],
        "recent_events": [
            event.model_dump(mode="json") for event in state.recent_events[-_MAX_EVENTS_IN_STATE:]
        ],
        "inventory": [
            item.model_dump(mode="json") for item in state.inventory[:_MAX_INVENTORY_IN_STATE]
        ],
        "unresolved_exits": [exit_.model_dump(mode="json") for exit_ in state.unresolved_exits],
        "pacing": pacing,
        "frontier": request.target_exit.model_dump(mode="json"),
        "prompt_hint": request.prompt_hint,
        "options": request.options.model_dump(mode="json") if request.options else None,
    }


def build_jev_questions(request: GenerationRequest) -> dict[str, JsonValue]:
    """The typed question set: choice, score and noul decisions in one call."""
    questions: dict[str, JsonValue] = {
        "room_type": {
            "type": "choice",
            "instructions": (
                "Which archetype should the new room behind `state.frontier` take, "
                "given `state.depth`, `state.recent_rooms`, `state.pacing`, "
                "`state.player` and `state.options`?"
            ),
            "criteria": {
                room_type.value: _ROOM_TYPE_CRITERIA[room_type]
                for room_type in _eligible_room_types(request)
            },
        },
        "size": {
            "type": "choice",
            "instructions": (
                "How large should the new room be for `state.depth` and its archetype "
                "(asked separately so pacing stays tunable)?"
            ),
            "criteria": {size.value: description for size, description in _SIZE_CRITERIA.items()},
        },
        "danger": {
            "type": "score",
            "instructions": (
                "How much danger should the new room hold for `state.player` "
                "(wounded players need breathing room) and `state.pacing`?"
            ),
            "criteria": list(_DANGER_LEVELS),
        },
        "enemy_density": {
            "type": "score",
            "instructions": "How crowded with enemies should the new room be?",
            "criteria": list(_ENEMY_LEVELS),
        },
        "loot_density": {
            "type": "score",
            "instructions": "How much loot should the new room offer?",
            "criteria": list(_LOOT_LEVELS),
        },
        "has_secret": {
            "type": "noul",
            "instructions": (
                "The new room should contain a hidden secret the player can discover "
                "(a concealed door, hidden cache, or mechanism)."
            ),
            "criteria": {
                "true": "A secret fits the pacing and rewards exploration.",
                "false": "No secret here; secrets belong elsewhere.",
            },
        },
        "exit_count": {
            "type": "choice",
            "instructions": (
                "How many additional exits, beyond the connection back through "
                "`state.frontier`, should the new room offer for future exploration?"
            ),
            "criteria": dict(_EXIT_COUNT_CRITERIA),
        },
        "atmosphere": {
            "type": "choice",
            "instructions": (
                "Which single environmental motif best gives this room a distinct identity? "
                "Prefer `none` when no motif strongly fits; use `state.recent_rooms` and "
                "`state.depth` to avoid repetition."
            ),
            "criteria": dict(_ATMOSPHERE_CRITERIA),
        },
    }
    return questions


# ---------------------------------------------------------------------------
# Answer decoding (typed accessors that never trust the upstream)
# ---------------------------------------------------------------------------


def _answer(answers: Mapping[str, Any], key: str, expected_type: str) -> Mapping[str, Any]:
    answer = answers.get(key)
    if not isinstance(answer, Mapping) or answer.get("type") != expected_type:
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION,
            f"jev answer {key!r} is missing or not a {expected_type} answer",
        )
    return answer


def _unit_number(value: Any, what: str) -> float:
    """A JSON number in [0, 1]; anything else is a schema violation.

    Python bools are ints, so they are rejected explicitly; NaN/Infinity
    (which ``json.loads`` happily parses from hostile bodies) are rejected by
    finiteness. Only float noise within ``_EPSILON`` of a boundary is tolerated.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is not finite")
    if number < -_EPSILON or number > 1.0 + _EPSILON:
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is outside [0, 1]")
    return min(max(number, 0.0), 1.0)


def _rubric_number(value: Any, maximum: float, what: str) -> float:
    """A JSON number within a rubric range [0, maximum], same rules."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is not a number")
    number = float(value)
    if not math.isfinite(number):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is not finite")
    if number < -_EPSILON or number > maximum + _EPSILON:
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION, f"jev answer {what} is outside its rubric range"
        )
    return min(max(number, 0.0), maximum)


def _probabilities(
    answer: Mapping[str, Any], allowed_keys: Container[str], key: str
) -> dict[str, float]:
    raw = answer.get("probabilities")
    if not isinstance(raw, Mapping):
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} has no probabilities map"
        )
    clean: dict[str, float] = {}
    for option, probability in raw.items():
        if not isinstance(option, str) or option not in allowed_keys:
            raise ProviderError(
                ErrorKind.SCHEMA_VIOLATION,
                f"jev answer {key!r} names probabilities outside the offered options",
            )
        clean[option] = round(_unit_number(probability, f"{key}.probabilities[{option!r}]"), 6)
    return clean


def _choice_answer(
    answers: Mapping[str, Any], key: str, allowed_keys: Container[str]
) -> tuple[str, float, dict[str, float]]:
    answer = _answer(answers, key, "choice")
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in allowed_keys:
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} chose outside the offered options"
        )
    probabilities = _probabilities(answer, allowed_keys, key)
    if "confidence" not in answer:
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} has no confidence value"
        )
    confidence = round(_unit_number(answer["confidence"], f"{key}.confidence"), 6)
    return choice, confidence, probabilities


def _score_answer(
    answers: Mapping[str, Any], key: str, levels: int
) -> tuple[float, float, dict[str, float]]:
    answer = _answer(answers, key, "score")
    if "score" not in answer:
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} has no score value")
    score = _rubric_number(answer["score"], float(levels - 1), f"{key}.score")
    probabilities = _probabilities(answer, {str(level) for level in range(levels)}, key)
    if "confidence" not in answer:
        raise ProviderError(
            ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} has no confidence value"
        )
    confidence = round(_unit_number(answer["confidence"], f"{key}.confidence"), 6)
    return score, confidence, probabilities


def _noul_answer(answers: Mapping[str, Any], key: str) -> float:
    answer = _answer(answers, key, "noul")
    if "noul" not in answer:
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, f"jev answer {key!r} has no noul value")
    return _unit_number(answer["noul"], f"{key}.noul")


def _sample_complete_choice(
    request: GenerationRequest,
    key: str,
    provider_choice: str,
    probabilities: Mapping[str, float],
    ordered_options: list[str],
) -> str:
    """Draw reproducibly from a complete calibrated Choice distribution.

    Some canned/older responses contain only the most likely probabilities.
    Those keep the provider's explicit choice. Current Jev responses include
    every offered option and sum to one; sampling that distribution stops a
    modest plurality from becoming the same room on every request while the
    request identity keeps replay output stable.
    """
    if set(probabilities) != set(ordered_options):
        return provider_choice
    total = sum(probabilities[option] for option in ordered_options)
    if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=0.01):
        return provider_choice

    identity = f"{request.run_id}:{request.request_id}:{key}".encode()
    unit = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") / float(1 << 64)
    point = unit * total
    cumulative = 0.0
    for option in ordered_options:
        cumulative += probabilities[option]
        if point < cumulative:
            return option
    return ordered_options[-1]


def compose_jev_room(
    request: GenerationRequest, answers: Mapping[str, Any], model: str
) -> tuple[RoomPlan, dict[str, JsonValue]]:
    """Compose shared Jev decisions into the director's canonical room contract."""
    options = request.options
    allow_secrets = options.allow_secrets if options else True

    eligible = [room_type.value for room_type in _eligible_room_types(request)]
    room_type_choice, room_type_confidence, room_type_probabilities = _choice_answer(
        answers, "room_type", eligible
    )
    sampled_room_type = _sample_complete_choice(
        request, "room_type", room_type_choice, room_type_probabilities, eligible
    )
    room_type = RoomType(sampled_room_type)  # membership already validated

    size_options = [size.value for size in RoomSize]
    size_choice, size_confidence, size_probabilities = _choice_answer(answers, "size", size_options)
    sampled_size = _sample_complete_choice(
        request, "size", size_choice, size_probabilities, size_options
    )
    size = RoomSize(sampled_size)

    danger_score, danger_confidence, danger_probabilities = _score_answer(
        answers, "danger", len(_DANGER_LEVELS)
    )
    danger = math.floor(danger_score + 0.5) + 1
    if options and options.max_danger is not None:
        danger = min(danger, options.max_danger)
    danger = min(max(danger, 1), 5)

    enemy_density, enemy_score, enemy_confidence, enemy_probabilities = _density_from_score(
        answers, "enemy_density", options.target_enemy_density if options else None
    )
    loot_density, loot_score, loot_confidence, loot_probabilities = _density_from_score(
        answers, "loot_density", options.target_loot_density if options else None
    )

    secret_decision = _noul_answer(answers, "has_secret")
    secret_probability = secret_decision if allow_secrets else 0.0
    has_secret = secret_probability >= _NOUL_TRUE_THRESHOLD

    extra_count, exit_count_confidence, exit_count_probabilities = _exit_count_answer(answers)
    exits = _compose_exits(request, room_type, extra_count)
    tags, atmosphere_choice, atmosphere_confidence, tag_probabilities = _compose_tags(answers)

    digest = hashlib.sha256(f"{request.run_id}:{request.request_id}:{model}".encode()).hexdigest()[
        :10
    ]
    description = _describe(size, room_type, tags)

    room = RoomPlan(
        room_id=f"jev-{digest}",
        depth=request.state.depth,
        room_type=room_type,
        size=size,
        danger=danger,
        exits=exits,
        enemy_density=enemy_density,
        loot_density=loot_density,
        secret_probability=round(secret_probability, 2),
        has_secret=has_secret,
        environmental_tags=tags,
        description=description,
    )
    metadata: dict[str, JsonValue] = {
        "room_type_confidence": room_type_confidence,
        "room_type_probabilities": room_type_probabilities,
        "room_type_provider_choice": room_type_choice,
        "size_confidence": size_confidence,
        "size_probabilities": size_probabilities,
        "size_provider_choice": size_choice,
        "danger_score": round(danger_score, 6),
        "danger_confidence": danger_confidence,
        "danger_probabilities": danger_probabilities,
        "enemy_density_score": round(enemy_score, 6),
        "enemy_density_confidence": enemy_confidence,
        "enemy_density_probabilities": enemy_probabilities,
        "loot_density_score": round(loot_score, 6),
        "loot_density_confidence": loot_confidence,
        "loot_density_probabilities": loot_probabilities,
        "has_secret_probability": round(secret_decision, 6),
        "secret_allowed": allow_secrets,
        "exit_count_confidence": exit_count_confidence,
        "exit_count_probabilities": exit_count_probabilities,
        "atmosphere_choice": atmosphere_choice,
        "atmosphere_confidence": atmosphere_confidence,
        "tag_probabilities": tag_probabilities,
        "exit_count": len(exits) - 1,
    }
    return room, metadata


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class CloudflareJevProvider(DungeonDirectorProvider):
    """Decides rooms by asking Jev typed questions through Cloudflare.

    One ``generate`` is one Cloudflare REST call (``/accounts/<id>/ai/run``)
    carrying every question in a single Jev request, then composing the typed
    answers into a validated :class:`RoomPlan`. All geometry stays game-side.
    """

    def __init__(self, config: JevConfig, transport: JevTransport | None = None) -> None:
        self._config = config
        self._injected_transport = transport
        self._transport: JevTransport | None = transport
        self.provider_id = CLOUDFLARE_JEV_PROVIDER_ID
        self.models = (config.model,)
        self.default_model = config.model

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        transport: JevTransport | None = None,
    ) -> CloudflareJevProvider:
        return cls(JevConfig.from_env(environ), transport)

    async def aclose(self) -> None:
        """Close the HTTP client if the provider owns it (tests/operators).

        Injected transports stay untouched: their owner decides their life.
        """
        if self._transport is not None and self._transport is not self._injected_transport:
            closer = getattr(self._transport, "aclose", None)
            if closer is not None:
                await closer()
        self._transport = self._injected_transport

    @property
    def availability(self) -> ProviderAvailability:
        if self._config.has_credentials:
            return ProviderAvailability(True)
        missing = " and ".join(
            name for name in REQUIRED_ENV_VARS if not getattr(self._config, _to_attr(name))
        )
        return ProviderAvailability(
            False,
            f"cloudflare-jev is registered but not configured: set {missing} to enable it",
        )

    def __repr__(self) -> str:  # credentials never reach repr
        configured = self._config.has_credentials
        return f"CloudflareJevProvider(model={self._config.model!r}, configured={configured})"

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        started = time.perf_counter()
        if not self._config.has_credentials:
            raise ProviderError(
                ErrorKind.PROVIDER_ERROR, "cloudflare-jev has no credentials configured"
            )
        if model not in self.models:
            raise ProviderError(ErrorKind.PROVIDER_ERROR, f"model {model!r} is not configured")

        body = {
            "model": model,
            "input": {
                "state": build_jev_state(request),
                "questions": build_jev_questions(request),
            },
        }
        # One shared lazily-created transport (and HTTP client) per provider:
        # never one AsyncClient per call. No retries regardless.
        if self._transport is None:
            self._transport = HttpxJevTransport()
        transport = self._transport
        transport_request = JevTransportRequest(
            method="POST",
            url=self._config.run_url,
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

        payload, cf_ray = _decode_success(response)
        answers, jev_model, usage = _jev_payload(payload)
        room, metadata = self._compose_room(request, answers, model)

        metadata.update(
            {
                "jev_model": jev_model,
                "upstream_http_status": 200,
                "adapter_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        )
        if cf_ray:
            metadata["cf_ray"] = cf_ray[:64]
        return ProviderResult(payload=room, usage=usage, provider_metadata=metadata)

    # --- composition -------------------------------------------------------

    def _compose_room(
        self, request: GenerationRequest, answers: Mapping[str, Any], model: str
    ) -> tuple[RoomPlan, dict[str, JsonValue]]:
        return compose_jev_room(request, answers, model)


def _to_attr(env_name: str) -> str:
    return env_name.removeprefix("CLOUDFLARE_").lower()


def _density_from_score(
    answers: Mapping[str, Any], key: str, target: float | None
) -> tuple[float, float, float, dict[str, float]]:
    score, confidence, probabilities = _score_answer(answers, key, len(_ENEMY_LEVELS))
    value = target if target is not None else score / float(len(_ENEMY_LEVELS) - 1)
    density = round(min(max(value, 0.0), 1.0), 2)
    return density, score, confidence, probabilities


def _exit_count_answer(answers: Mapping[str, Any]) -> tuple[int, float, dict[str, float]]:
    choice, confidence, probabilities = _choice_answer(answers, "exit_count", _EXIT_COUNT_CRITERIA)
    return int(choice), confidence, probabilities


def _compose_exits(
    request: GenerationRequest,
    room_type: RoomType,
    extra_count: int,
) -> list[Exit]:
    frontier = request.target_exit.direction
    back_kind = ExitKind.STAIRS if frontier in _VERTICAL else ExitKind.DOOR
    exits = [Exit(direction=_OPPOSITE[frontier], kind=back_kind, locked=False)]

    free = [direction for direction in _CARDINALS if direction != exits[0].direction]
    # Jev decides how many branches the room should expose, while geometry
    # remains code-owned. Do not map that count onto the enum's fixed order:
    # with the common one-branch answer it made nearly every new door point
    # north. A request-stable permutation keeps replays deterministic while
    # distributing directions across a run.
    identity = (
        f"{request.run_id}:{request.request_id}:"
        f"{request.target_exit.room_id}:{request.target_exit.direction.value}"
    )
    free.sort(
        key=lambda direction: hashlib.sha256(f"{identity}:{direction.value}".encode()).digest()
    )
    for direction in free[: min(max(extra_count, 0), len(free))]:
        if room_type is RoomType.CORRIDOR:
            kind = ExitKind.PASSAGE
        else:
            kind = ExitKind.DOOR
        exits.append(Exit(direction=direction, kind=kind, locked=False))
    return exits


def _compose_tags(
    answers: Mapping[str, Any],
) -> tuple[list[EnvironmentalTag], str, float, dict[str, float]]:
    choice, confidence, probabilities = _choice_answer(
        answers, "atmosphere", list(_ATMOSPHERE_CRITERIA)
    )
    tags = [] if choice == "none" else [EnvironmentalTag(choice)]
    return tags, choice, confidence, probabilities


def _describe(size: RoomSize, room_type: RoomType, tags: list[EnvironmentalTag]) -> str:
    text = f"{size.value.capitalize()} {room_type.value.replace('_', ' ')}"
    if tags:
        text += ", " + " and ".join(tag.value for tag in tags)
    return (text + ".")[:200]


# ---------------------------------------------------------------------------
# Upstream response handling
# ---------------------------------------------------------------------------


def _transport_error(exc: Exception) -> ProviderError:
    # Message carries the exception type only: text and tracebacks can echo
    # the request (which contains the credential headers' target URL).
    try:
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return ProviderError(
                ErrorKind.PROVIDER_TIMEOUT, "cloudflare request timed out at the transport layer"
            )
        if isinstance(exc, httpx.HTTPError):
            return ProviderError(
                ErrorKind.PROVIDER_ERROR,
                f"cloudflare request failed at the transport layer ({type(exc).__name__})",
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
            f"cloudflare rejected the credentials (HTTP {status})",
        )
    if status == 429:
        return ProviderError(ErrorKind.RATE_LIMITED, "cloudflare rate limit reached (HTTP 429)")
    if status == 404:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, "cloudflare reported HTTP 404 (unknown account or model)"
        )
    if status in (400, 422):
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"cloudflare rejected the request body (HTTP {status})"
        )
    if status >= 500:
        return ProviderError(
            ErrorKind.PROVIDER_ERROR, f"cloudflare reported an upstream error (HTTP {status})"
        )
    return ProviderError(ErrorKind.PROVIDER_ERROR, f"cloudflare returned HTTP {status}")


def _decode_success(response: JevTransportResponse) -> tuple[Any, str | None]:
    """Decode a 200 body: unwrap the v4 envelope, return the Jev payload.

    The catalog page for the model shows the bare Jev payload as the response;
    the REST API documentation shows model output inside the standard
    ``{"result": ..., "success": ...}`` v4 envelope. Both are accepted: the
    envelope when present, the bare payload otherwise.
    """
    try:
        body = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderError(
            ErrorKind.INVALID_JSON, "cloudflare response body was not valid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "cloudflare response was not a JSON object")

    cf_ray = None
    for name, value in response.headers.items():
        if name.lower() == "cf-ray" and isinstance(value, str):
            cf_ray = value

    if "success" in body and ("result" in body or "errors" in body):
        if body.get("success") is not True:
            raise ProviderError(
                ErrorKind.PROVIDER_ERROR,
                f"cloudflare response envelope reported failure "
                f"(errors: {len(body.get('errors') or [])})",
            )
        return body.get("result"), cf_ray
    return body, cf_ray


def _jev_payload(payload: Any) -> tuple[Mapping[str, Any], str, UsageStats | None]:
    """Validate the Jev payload shape; never trust field types."""
    if not isinstance(payload, Mapping):
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "jev payload was not a JSON object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping) or not answers:
        raise ProviderError(ErrorKind.SCHEMA_VIOLATION, "jev payload has no answers object")
    jev_model = payload.get("model")
    safe_model = jev_model[:64] if isinstance(jev_model, str) else ""
    usage = None
    raw_usage = payload.get("usage")
    if isinstance(raw_usage, Mapping):
        input_tokens = raw_usage.get("input_tokens")
        output_tokens = raw_usage.get("output_tokens")
        usage = UsageStats(
            input_tokens=_usage_int(input_tokens),
            output_tokens=_usage_int(output_tokens),
        )
    return answers, safe_model, usage


def decode_jev_payload(payload: Any) -> tuple[Mapping[str, Any], str, UsageStats | None]:
    """Validate a bare Jev response shared by Cloudflare and TypeSafe transports."""
    return _jev_payload(payload)


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return min(max(value, 0), 10_000_000)
