"""Canonical dungeon director contracts (issue #3).

Versioned, provider-independent data contracts shared by the Godot game,
the FastAPI director service, and the benchmark tooling.

Design rules (from epic #1 / issue #3):

* Model output is *semantic* (room type, density, danger, exits), never
  tile-level geometry. Geometry is derived deterministically game-side.
* Required fields are minimal; optional fields provide forward compatibility.
  Unknown fields are rejected (``extra="forbid"``) so accidental provider
  leakage surfaces immediately instead of contaminating game logic.
* Provider-specific payloads live only inside
  :attr:`ResponseMetadata.provider_metadata` (max 32 keys and 8192
  serialized UTF-8 bytes).
* All values are bounded (ranges, lengths, list sizes) so a hostile or
  broken provider response can never inject unbounded data into the game.
* ``contract_version`` is required on every envelope; a wrong or malformed
  version fails validation with the stable Pydantic error type
  ``unsupported_contract_version``.
* Timestamps are timezone-aware ISO 8601 only; naive values fail validation
  normally (never a raw ``TypeError``).

The machine-readable JSON Schemas and example fixtures are exported to
``contracts/`` at the repository root; see ``contracts/README.md``.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

__all__ = [
    "CONTRACT_VERSION",
    "MAX_PROVIDER_METADATA_JSON_BYTES",
    "SUPPORTED_CONTRACT_MAJOR",
    "UNKNOWN_PROVIDER",
    "UNKNOWN_REQUEST_ID",
    "UNKNOWN_RUN_ID",
    "EnvironmentalTag",
    "ErrorKind",
    "Exit",
    "ExitDirection",
    "ExitKind",
    "GenerationOptions",
    "GenerationRequest",
    "GenerationResponse",
    "HungerState",
    "InventoryItem",
    "PacingContext",
    "PlayerState",
    "RecentEvent",
    "ResponseMetadata",
    "RoomPlan",
    "RoomSize",
    "RoomType",
    "UnresolvedExit",
    "UsageStats",
    "VisitedRoom",
]

#: Contract version. Bump the major when a change breaks existing consumers;
#: the game and director reject envelopes whose major differs.
CONTRACT_VERSION = "1.0.0"

SUPPORTED_CONTRACT_MAJOR = int(CONTRACT_VERSION.split(".")[0])

#: Schema identifier prefix used in exported JSON Schemas.
SCHEMA_ID_PREFIX = "https://mule.github.io/ai-hack-poc/contracts"

#: Sentinels used by :meth:`GenerationResponse.failure` when caller-supplied
#: ids/names are not valid contract values (see contracts/README.md).
UNKNOWN_REQUEST_ID = "unknown-request-id"
UNKNOWN_RUN_ID = "unknown-run-id"
UNKNOWN_PROVIDER = "unknown"

#: Upper bound for the compact UTF-8 serialized JSON of provider_metadata.
MAX_PROVIDER_METADATA_JSON_BYTES = 8192

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


# ---------------------------------------------------------------------------
# Shared annotated field types (bounded primitives)
# ---------------------------------------------------------------------------

BoundedId = Annotated[str, Field(pattern=_ID_RE.pattern, min_length=1, max_length=64)]
ShortText = Annotated[str, Field(min_length=1, max_length=128)]
UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]


# ---------------------------------------------------------------------------
# Sanitizers used by GenerationResponse.failure (arbitrary provider errors)
# ---------------------------------------------------------------------------


def _sanitize_id(value: str | None, sentinel: str) -> str:
    return value if isinstance(value, str) and _ID_RE.match(value) else sentinel


def _sanitize_name(value: str | None) -> str:
    if isinstance(value, str) and value.strip():
        return value[:128]
    return UNKNOWN_PROVIDER


def _sanitize_code(value: ErrorKind | str) -> ErrorKind:
    if isinstance(value, ErrorKind):
        return value
    try:
        return ErrorKind(value)
    except ValueError:
        return ErrorKind.PROVIDER_ERROR


def _sanitize_message(value: str | None, code: ErrorKind) -> str:
    text = value.strip() if isinstance(value, str) and value.strip() else ""
    if not text:
        text = f"Generation failed ({code.value})."
    return text[:500]


def _sanitize_raw_excerpt(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)[:4096]


def _normalize_timestamp(value: datetime | None, fallback: datetime) -> datetime:
    if value is None:
        return fallback
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _sanitize_provider_metadata(value: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    """Return ``value`` only if it satisfies the provider_metadata bounds.

    Over-budget or malformed values are replaced with ``{}`` so that
    :meth:`GenerationResponse.failure` can never fail validation.
    """
    if not isinstance(value, dict) or len(value) > 32:
        return {}
    try:
        return _provider_metadata_within_byte_budget(value)
    except ValueError:
        return {}


def _check_contract_version(value: str) -> str:
    """Emit a stable ``unsupported_contract_version`` validation error type.

    Both malformed version strings and future major versions surface as the
    same machine-checkable Pydantic error type inside a normal
    ``ValidationError``; nothing escapes model validation.
    """
    if not _VERSION_RE.match(value):
        raise PydanticCustomError(
            "unsupported_contract_version",
            "contract_version must be semver-like MAJOR.MINOR.PATCH, got {value!r}",
            {"value": value},
        )
    major = int(value.split(".")[0])
    if major != SUPPORTED_CONTRACT_MAJOR:
        raise PydanticCustomError(
            "unsupported_contract_version",
            "unsupported contract version {value!r}; this build speaks {supported} (major {major})",
            {"value": value, "supported": CONTRACT_VERSION, "major": SUPPORTED_CONTRACT_MAJOR},
        )
    return value


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RoomType(StrEnum):
    """Semantic room archetypes. Never encodes geometry."""

    ENTRANCE = "entrance"
    ROOM = "room"
    CORRIDOR = "corridor"
    CAVERN = "cavern"
    CHAMBER = "chamber"
    VAULT = "vault"
    SHRINE = "shrine"
    SHOP = "shop"
    TREASURE = "treasure"
    STAIRS_DOWN = "stairs_down"
    STAIRS_UP = "stairs_up"


class RoomSize(StrEnum):
    """Relative size class; exact tile extents are decided game-side."""

    TINY = "tiny"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    HUGE = "huge"


class ExitDirection(StrEnum):
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"
    UP = "up"
    DOWN = "down"


class ExitKind(StrEnum):
    DOOR = "door"
    PASSAGE = "passage"
    STAIRS = "stairs"
    SECRET = "secret"


class EnvironmentalTag(StrEnum):
    DARK = "dark"
    FLOODED = "flooded"
    FUNGAL = "fungal"
    ICY = "icy"
    HOT = "hot"
    RUINED = "ruined"
    HALLOWED = "hallowed"
    TRAPPED = "trapped"
    OVERGROWN = "overgrown"
    NOISY = "noisy"


class HungerState(StrEnum):
    SATIATED = "satiated"
    NORMAL = "normal"
    HUNGRY = "hungry"
    WEAK = "weak"
    STARVING = "starving"


class ErrorKind(StrEnum):
    """Machine-readable failure codes for the documented failure path."""

    SCHEMA_VIOLATION = "schema_violation"
    INVALID_JSON = "invalid_json"
    UNSUPPORTED_CONTRACT_VERSION = "unsupported_contract_version"
    EMPTY_RESPONSE = "empty_response"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_TIMEOUT = "provider_timeout"
    RATE_LIMITED = "rate_limited"
    SAFETY_REFUSAL = "safety_refusal"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERNAL_ERROR = "internal_error"


# ---------------------------------------------------------------------------
# DungeonState graph (the game -> director payload)
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    """Base config: reject unknown fields, use modern pydantic v2 defaults."""

    model_config = ConfigDict(extra="forbid")


class PlayerState(_StrictModel):
    hp: Annotated[int, Field(ge=0, le=99999)]
    max_hp: Annotated[int, Field(ge=1, le=99999)]
    level: Annotated[int, Field(ge=1, le=100)] = 1
    hunger: HungerState | None = None
    conditions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=16)
    ] = []

    @model_validator(mode="after")
    def _hp_within_max(self) -> PlayerState:
        if self.hp > self.max_hp:
            raise ValueError(f"hp ({self.hp}) must not exceed max_hp ({self.max_hp})")
        return self


class VisitedRoom(_StrictModel):
    """Summary of an already-committed room, for director context only."""

    room_id: BoundedId
    room_type: RoomType
    danger: Annotated[int, Field(ge=1, le=5)] = 1


class RecentEvent(_StrictModel):
    event: ShortText
    turn: Annotated[int, Field(ge=0)] | None = None


class InventoryItem(_StrictModel):
    item_id: BoundedId
    quantity: Annotated[int, Field(ge=1, le=9999)] = 1
    category: Annotated[str, Field(min_length=1, max_length=32)] | None = None


class UnresolvedExit(_StrictModel):
    """A committed exit whose destination room is not generated yet."""

    room_id: BoundedId
    direction: ExitDirection
    since_turn: Annotated[int, Field(ge=0)] | None = None


class PacingContext(_StrictModel):
    """Aggregate pacing signals; all optional so older clients stay valid."""

    rooms_on_depth: Annotated[int, Field(ge=0, le=1024)] = 0
    secrets_found: Annotated[int, Field(ge=0, le=1024)] = 0
    encounters_on_depth: Annotated[int, Field(ge=0, le=1024)] = 0
    turns_on_depth: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    average_recent_danger: Annotated[float, Field(ge=0.0, le=5.0)] | None = None


class DungeonState(_StrictModel):
    """Snapshot of committed, player-visible dungeon state (semantic only)."""

    depth: Annotated[int, Field(ge=1, le=128)]
    turn: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    player: PlayerState
    recent_rooms: Annotated[list[VisitedRoom], Field(max_length=32)] = []
    recent_events: Annotated[list[RecentEvent], Field(max_length=32)] = []
    inventory: Annotated[list[InventoryItem], Field(max_length=128)] = []
    unresolved_exits: Annotated[list[UnresolvedExit], Field(max_length=64)] = []
    pacing: PacingContext | None = None


class GenerationOptions(_StrictModel):
    """Optional hints the game may attach to a generation request."""

    max_danger: Annotated[int, Field(ge=1, le=5)] | None = None
    forbidden_room_types: Annotated[list[RoomType], Field(max_length=8)] = []
    allow_secrets: bool = True
    target_enemy_density: UnitFloat | None = None
    target_loot_density: UnitFloat | None = None


# ---------------------------------------------------------------------------
# RoomPlan (the director -> game payload)
# ---------------------------------------------------------------------------


class Exit(_StrictModel):
    direction: ExitDirection
    kind: ExitKind = ExitKind.DOOR
    locked: bool = False


class RoomPlan(_StrictModel):
    """Semantic description of one room. Geometry is derived game-side."""

    room_id: BoundedId
    depth: Annotated[int, Field(ge=1, le=128)]
    room_type: RoomType
    size: RoomSize
    danger: Annotated[int, Field(ge=1, le=5)] = 1
    exits: Annotated[list[Exit], Field(max_length=8)] = []
    enemy_density: UnitFloat = 0.0
    loot_density: UnitFloat = 0.0
    secret_probability: UnitFloat = 0.0
    has_secret: bool | None = None
    environmental_tags: Annotated[list[EnvironmentalTag], Field(max_length=8)] = []
    description: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def _semantic_invariants(self) -> RoomPlan:
        directions = [exit_.direction for exit_ in self.exits]
        if len(set(directions)) != len(directions):
            raise ValueError("exits must have unique directions")
        if self.has_secret is True and self.secret_probability <= 0.0:
            raise ValueError("has_secret=true requires secret_probability > 0")
        return self


# ---------------------------------------------------------------------------
# Envelope metadata
# ---------------------------------------------------------------------------


class ErrorDetail(_StrictModel):
    """Structured failure information for the documented failure path."""

    code: ErrorKind
    message: Annotated[str, Field(min_length=1, max_length=500)]
    raw_excerpt: Annotated[str, Field(min_length=0, max_length=4096)] | None = None
    occurred_at: AwareDatetime | None = None


class UsageStats(_StrictModel):
    input_tokens: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    output_tokens: Annotated[int, Field(ge=0, le=10_000_000)] | None = None
    estimated_cost_usd: Annotated[float, Field(ge=0.0)] | None = None


def _provider_metadata_within_byte_budget(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    serialized = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(serialized) > MAX_PROVIDER_METADATA_JSON_BYTES:
        raise ValueError(
            f"provider_metadata serialized JSON exceeds {MAX_PROVIDER_METADATA_JSON_BYTES} "
            f"UTF-8 bytes (got {len(serialized)})"
        )
    return value


class ResponseMetadata(_StrictModel):
    """Telemetry envelope. Provider-specific blobs live in provider_metadata.

    All timestamps must be timezone-aware ISO 8601 values; naive datetimes
    are a normal validation failure.
    """

    provider: Annotated[str, Field(min_length=1, max_length=128)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    started_at: AwareDatetime
    completed_at: AwareDatetime
    latency_ms: Annotated[float, Field(ge=0.0)] | None = None
    usage: UsageStats | None = None
    error: ErrorDetail | None = None
    provider_metadata: Annotated[
        dict[str, JsonValue],
        Field(max_length=32),
        AfterValidator(_provider_metadata_within_byte_budget),
    ] = {}

    @model_validator(mode="after")
    def _completed_after_started(self) -> ResponseMetadata:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        return self


# ---------------------------------------------------------------------------
# Request / response envelopes
# ---------------------------------------------------------------------------


class GenerationRequest(_StrictModel):
    """Game -> director: generate the room behind an unresolved exit.

    ``contract_version`` and ``target_exit`` are required on the wire;
    ``target_exit`` identifies the frontier being generated and must exactly
    match one entry of ``state.unresolved_exits``.
    """

    contract_version: str
    request_id: BoundedId
    run_id: BoundedId
    state: DungeonState
    target_exit: UnresolvedExit
    prompt_hint: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    options: GenerationOptions | None = None

    _check_version = field_validator("contract_version", mode="after")(_check_contract_version)

    @model_validator(mode="after")
    def _target_exit_on_frontier(self) -> GenerationRequest:
        if self.target_exit not in self.state.unresolved_exits:
            raise ValueError(
                f"target_exit ({self.target_exit.room_id} / {self.target_exit.direction.value}) "
                "must exactly match an entry in state.unresolved_exits"
            )
        return self


class GenerationResponse(_StrictModel):
    """Director -> game: one semantic room decision plus telemetry."""

    contract_version: str
    request_id: BoundedId
    run_id: BoundedId
    success: bool = True
    room: RoomPlan | None = None
    metadata: ResponseMetadata

    _check_version = field_validator("contract_version", mode="after")(_check_contract_version)

    @model_validator(mode="after")
    def _success_invariant(self) -> GenerationResponse:
        if self.success:
            if self.room is None:
                raise ValueError("successful response must carry a room plan")
            if self.metadata.error is not None:
                raise ValueError("successful response must not carry error detail")
        else:
            if self.room is not None:
                raise ValueError("failed response must not carry a room plan")
            if self.metadata.error is None:
                raise ValueError("failed response must carry error detail")
        return self

    @classmethod
    def success_from_request(
        cls,
        request: GenerationRequest,
        *,
        room: RoomPlan,
        provider: str,
        model: str,
        started_at: datetime,
        completed_at: datetime,
        usage: UsageStats | None = None,
        provider_metadata: dict[str, JsonValue] | None = None,
    ) -> GenerationResponse:
        """Build a well-formed success envelope for a request.

        Raises ``ValueError`` when the room depth does not match the request
        state depth (a generation decision for another frontier).
        """
        if room.depth != request.state.depth:
            raise ValueError(
                f"room.depth ({room.depth}) must equal request.state.depth ({request.state.depth})"
            )
        return cls(
            contract_version=CONTRACT_VERSION,
            request_id=request.request_id,
            run_id=request.run_id,
            success=True,
            room=room,
            metadata=ResponseMetadata(
                provider=provider,
                model=model,
                started_at=started_at,
                completed_at=completed_at,
                usage=usage,
                provider_metadata=provider_metadata or {},
            ),
        )

    @classmethod
    def failure(
        cls,
        *,
        request_id: str | None,
        run_id: str | None,
        provider: str | None,
        model: str | None,
        code: ErrorKind | str,
        message: str | None,
        raw_excerpt: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        usage: UsageStats | None = None,
        provider_metadata: dict[str, JsonValue] | None = None,
    ) -> GenerationResponse:
        """Build the canonical failure envelope for arbitrary provider errors.

        This constructor never fails validation: every field is sanitized to a
        valid value first (see contracts/README.md):

        * ``request_id``/``run_id``: invalid values become the documented
          sentinels ``unknown-request-id``/``unknown-run-id``.
        * ``provider``/``model``: empty/invalid become ``"unknown"``;
          overlong values are truncated to 128 characters.
        * ``message``: empty becomes a generic fallback naming the error
          code; anything longer than 500 characters is truncated.
        * ``raw_excerpt``: coerced to ``str`` and truncated to 4096 chars
          (``None`` stays ``None``).
        * ``code``: unknown values fall back to ``provider_error``.
        * ``provider_metadata``: malformed, over 32 keys, or over the
          serialized byte budget becomes ``{}``.
        * timestamps: naive values are normalized to UTC; ``completed_at``
          is clamped to ``started_at`` when it would precede it.

        ``room`` is always ``None`` here; the game falls back to the rules
        baseline when it sees ``success == false``.
        """
        now = datetime.now(UTC)
        started = _normalize_timestamp(started_at, now)
        completed = max(_normalize_timestamp(completed_at, now), started)
        safe_code = _sanitize_code(code)
        metadata = ResponseMetadata(
            provider=_sanitize_name(provider),
            model=_sanitize_name(model),
            started_at=started,
            completed_at=completed,
            usage=usage,
            error=ErrorDetail(
                code=safe_code,
                message=_sanitize_message(message, safe_code),
                raw_excerpt=_sanitize_raw_excerpt(raw_excerpt),
                occurred_at=completed,
            ),
            provider_metadata=_sanitize_provider_metadata(provider_metadata),
        )
        return cls(
            contract_version=CONTRACT_VERSION,
            request_id=_sanitize_id(request_id, UNKNOWN_REQUEST_ID),
            run_id=_sanitize_id(run_id, UNKNOWN_RUN_ID),
            success=False,
            room=None,
            metadata=metadata,
        )
