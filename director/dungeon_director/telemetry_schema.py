"""Versioned OpenLIT telemetry schema and correlation contract (issue #22).

This module owns the game-side wire contract for the future
``POST /v1/telemetry/game`` bridge (``GameEvent`` / ``GameEventBatch``) and the
shared allowlist that keeps metric cardinality bounded across the director
and the game. See ``observability/telemetry-schema.md`` for the full
human-readable reference table (spans, events, metrics, resource attributes,
correlation rules, and worked examples per provider).

It does not touch ``dungeon_director.telemetry`` (director-side spans and
metrics, owned by issue #11/#23) or implement the bridge endpoint itself
(owned by a later task); it only imports the director's
:data:`~dungeon_director.telemetry.METRIC_DIMENSIONS` read-only, so the two
allowlists can be reconciled by a single test instead of drifting apart.

Design rules, all covered by ``tests/test_telemetry_schema.py``:

* **Every attribute is allowlisted per event.** ``GameEvent`` rejects any
  attribute key that is not in that event's allowlist, and rejects a value of
  the wrong type or out of range. There is no free-text escape hatch.
* **Correlation IDs and measurements are never metric dimensions.**
  ``run_id``, ``request_id`` and every id-shaped attribute (``frontier_id``,
  ``room_id``, ``shadow_comparison_id``, ``replay_id``) are high-cardinality
  by construction; every duration and density attribute (``queue_ms``,
  ``time_to_entry_ms``, ``enemy_density``, ...) is a continuous observation,
  not a label. :func:`assert_safe_metric_dimensions` raises, with a distinct
  message for each case, if either kind is used as a metric dimension key.
* **No raw exceptions, prompts or credentials.** String attribute values are
  bounded in length, must be single-line, and are rejected outright if they
  look secret-shaped (``sk-...``, ``Bearer ...``, ``Authorization:``, ...).
  Free-form provider/game text never reaches this schema; only short,
  label-shaped values do.
* **Sanitize-then-validate is the safe path for real traffic.**
  :func:`sanitize_attributes` never raises: it drops whatever does not fit
  the contract. ``GameEvent`` itself is strict (raises on a violation) so
  the contract is unambiguous and testable. A future bridge sanitizes first
  and constructs ``GameEvent`` from the result, which then never raises.
* **The contract is versioned.** :data:`SCHEMA_VERSION` is a required field
  on every batch; a mismatched value fails validation so a dashboard or
  bridge can detect an incompatible producer instead of silently
  misinterpreting fields.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from dungeon_director.contracts import BoundedId, ExitDirection, RoomSize, RoomType
from dungeon_director.telemetry import METRIC_DIMENSIONS as DIRECTOR_METRIC_DIMENSIONS

__all__ = [
    "ATTRIBUTE_REGISTRY",
    "CORRELATION_ONLY_KEYS",
    "EVENT_ATTRIBUTE_ALLOWLIST",
    "GAME_METRIC_DIMENSIONS",
    "MAX_ATTRIBUTES_PER_EVENT",
    "MAX_ATTRIBUTE_STRING_LENGTH",
    "MAX_EVENTS_PER_BATCH",
    "MEASUREMENT_ONLY_KEYS",
    "SCHEMA_VERSION",
    "AttributeSpec",
    "AttributeType",
    "CardinalityClass",
    "GameEvent",
    "GameEventBatch",
    "GameEventName",
    "assert_safe_metric_dimensions",
    "sanitize_attributes",
]

#: The ``schema_version`` every ``GameEventBatch`` must carry. Bump this (and
#: document a migration path in telemetry-schema.md) on any breaking change
#: to event names, required fields, or the attribute registry below.
SCHEMA_VERSION = "1"

#: Hard cap on events per ``POST /v1/telemetry/game`` batch. Bounds request
#: size and, transitively, how much a single HTTP call can fan out into spans
#: and log records at the collector.
MAX_EVENTS_PER_BATCH = 100

#: Hard cap on attributes per event, independent of how many keys an
#: individual event's allowlist names (all are well under this today).
MAX_ATTRIBUTES_PER_EVENT = 16

#: Upper bound for a string attribute value. Long enough for a label, far too
#: short for a prompt, a stack trace, or a raw provider payload.
MAX_ATTRIBUTE_STRING_LENGTH = 128

# Same shape as dungeon_director.contracts.BoundedId / telemetry._ID_RE.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# frontier_id is game-composed as "<room_id>:<direction>" (e.g. "r-000:east"),
# so it needs the colon BoundedId forbids; bounded to 128 chars, not 64.
_FRONTIER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
# Same shape as dungeon_director.telemetry._LABEL_RE.
_LABEL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}$")
# W3C Trace Context traceparent: version-traceid-spanid-flags, all lowercase hex.
_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
# Case-insensitive: credential- or bearer-token-shaped strings are never a valid attribute.
_SECRET_LIKE_RE = re.compile(
    r"(?i)(bearer\s+\S|sk-[a-z0-9]{4,}|api[_-]?key|authorization\s*:|password\s*[:=])"
)


class GameEventName(StrEnum):
    """Game-side lifecycle stages of one generation decision (issue #21).

    Director- and provider-side stages (provider selection, the provider
    call, response parsing/validation) are director spans, not game events;
    see telemetry-schema.md for the full trace tree. These are the stages
    the game itself observes and reports, in order:
    ``frontier.discovered`` -> ``generation.queued`` -> ``generation.sent``
    -> ``generation.response_received`` -> one of ``generation.accepted`` /
    ``generation.normalized`` / ``generation.rejected`` (rejection is
    followed by ``generation.fallback_applied``) -> ``room.committed`` ->
    ``door.revealed`` -> ``room.entered``.
    """

    FRONTIER_DISCOVERED = "frontier.discovered"
    GENERATION_QUEUED = "generation.queued"
    GENERATION_SENT = "generation.sent"
    GENERATION_RESPONSE_RECEIVED = "generation.response_received"
    GENERATION_ACCEPTED = "generation.accepted"
    GENERATION_NORMALIZED = "generation.normalized"
    GENERATION_REJECTED = "generation.rejected"
    GENERATION_FALLBACK_APPLIED = "generation.fallback_applied"
    ROOM_COMMITTED = "room.committed"
    DOOR_REVEALED = "door.revealed"
    ROOM_ENTERED = "room.entered"


#: Events reported before a director request exists (and thus before
#: ``request_id`` is known): discovering the frontier and queueing the
#: request. ``generation.sent`` requires it -- the game must have assigned
#: it to build the outgoing ``GenerationRequest``.
_EVENTS_WITHOUT_REQUEST_ID = frozenset(
    {GameEventName.FRONTIER_DISCOVERED, GameEventName.GENERATION_QUEUED}
)


class CardinalityClass(StrEnum):
    """Whether and how an attribute may be used as a metric dimension.

    ``LOW``: a small, closed set of values (an enum, a bounded int, a bool,
    or a label matched against :data:`_LABEL_RE`) -- safe as a metric
    dimension (a label OpenLIT groups and aggregates by).
    ``CORRELATION``: an id that is unique (or near-unique) per event --
    valid on spans/logs only, rejected by :func:`assert_safe_metric_dimensions`.
    ``MEASUREMENT``: a continuous numeric observation (a duration or a
    density) -- this is what a histogram *records*, never a label a metric
    is grouped by; also rejected by :func:`assert_safe_metric_dimensions`,
    with a distinct message, so a duration never accidentally becomes an
    unbounded-cardinality dimension.
    """

    LOW = "low"
    CORRELATION = "correlation"
    MEASUREMENT = "measurement"


class AttributeType(StrEnum):
    STRING = "string"
    ENUM = "enum"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ID = "id"


@dataclass(frozen=True, slots=True)
class AttributeSpec:
    """Validation rule for one attribute key, shared by every event that allows it."""

    type: AttributeType
    cardinality: CardinalityClass
    allowed_values: frozenset[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: Overrides :data:`_ID_RE` for ``AttributeType.ID`` keys whose shape
    #: differs from a plain ``BoundedId`` (e.g. ``frontier_id``, which is
    #: ``<room_id>:<direction>`` and so must allow a colon).
    pattern: re.Pattern[str] | None = None


_NORMALIZE_REASONS = frozenset(
    {
        "danger_clamped",
        "room_type_forbidden",
        "exit_conflict",
        "exit_direction_reassigned",
        "secret_probability_clamped",
        "exit_pruned",
    }
)
#: duplicate_room_id / placement_failure are blocking: the game cannot safely
#: adjust its way out of them (unlike exit_pruned, a normalize case), so the
#: room is rejected outright and a fallback follows.
_REJECT_REASONS = frozenset(
    {"schema_invalid", "policy_violation", "empty_room", "duplicate_room_id", "placement_failure"}
)
#: Mirrors dungeon_director.telemetry.Outcome for the error-shaped cases, plus
#: transport_failure (the game's own HTTP/network layer failed before a
#: director response was ever received) and rejected_by_game, both of which
#: only exist game-side (the director never sees either).
_FALLBACK_REASONS = frozenset(
    {
        "provider_error",
        "provider_timeout",
        "schema_error",
        "selection_error",
        "rejected_by_game",
        "transport_failure",
    }
)
_EXECUTION_MODES = frozenset({"active", "shadow", "replay"})

#: Attribute keys allowed on every event regardless of ``event_name``.
COMMON_ATTRIBUTE_KEYS = frozenset({"shadow_comparison_id", "replay_id", "execution_mode"})

#: Every attribute key this schema knows about, and how to validate it. A key
#: absent here can never appear in a sanitized or validated event, even if an
#: event's allowlist names it (guarded by a consistency test).
#: Shared bound for every *_ms timing attribute: long enough for a
#: pathologically slow request, far too short to be an unbounded overflow.
_MAX_DURATION_MS = 3_600_000.0

ATTRIBUTE_REGISTRY: dict[str, AttributeSpec] = {
    "frontier_id": AttributeSpec(
        AttributeType.ID, CardinalityClass.CORRELATION, pattern=_FRONTIER_ID_RE
    ),
    "room_id": AttributeSpec(AttributeType.ID, CardinalityClass.CORRELATION),
    "shadow_comparison_id": AttributeSpec(AttributeType.ID, CardinalityClass.CORRELATION),
    "replay_id": AttributeSpec(AttributeType.ID, CardinalityClass.CORRELATION),
    "depth": AttributeSpec(AttributeType.INT, CardinalityClass.LOW, minimum=1, maximum=128),
    "danger": AttributeSpec(AttributeType.INT, CardinalityClass.LOW, minimum=1, maximum=5),
    "exit_direction": AttributeSpec(
        AttributeType.ENUM,
        CardinalityClass.LOW,
        allowed_values=frozenset(d.value for d in ExitDirection),
    ),
    "room_type": AttributeSpec(
        AttributeType.ENUM,
        CardinalityClass.LOW,
        allowed_values=frozenset(t.value for t in RoomType),
    ),
    "room_size": AttributeSpec(
        AttributeType.ENUM,
        CardinalityClass.LOW,
        allowed_values=frozenset(s.value for s in RoomSize),
    ),
    "provider": AttributeSpec(AttributeType.STRING, CardinalityClass.LOW),
    "model": AttributeSpec(AttributeType.STRING, CardinalityClass.LOW),
    "normalize_reason": AttributeSpec(
        AttributeType.ENUM, CardinalityClass.LOW, allowed_values=_NORMALIZE_REASONS
    ),
    "reject_reason": AttributeSpec(
        AttributeType.ENUM, CardinalityClass.LOW, allowed_values=_REJECT_REASONS
    ),
    "fallback_reason": AttributeSpec(
        AttributeType.ENUM, CardinalityClass.LOW, allowed_values=_FALLBACK_REASONS
    ),
    "execution_mode": AttributeSpec(
        AttributeType.ENUM, CardinalityClass.LOW, allowed_values=_EXECUTION_MODES
    ),
    # Room-behavior observations (issue #21: "what kinds of rooms does each
    # model choose"). exit_count/has_secret are small closed sets -- safe
    # dimensions. enemy_density/loot_density are continuous UnitFloat values
    # (RoomPlan) -- measurements, not labels, same as the timing fields below.
    "exit_count": AttributeSpec(AttributeType.INT, CardinalityClass.LOW, minimum=0, maximum=8),
    "has_secret": AttributeSpec(AttributeType.BOOL, CardinalityClass.LOW),
    "enemy_density": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=1.0
    ),
    "loot_density": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=1.0
    ),
    # Timing observations: each is a histogram-shaped measurement, never a
    # metric dimension (a continuous float cannot be a bounded label).
    "queue_ms": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=_MAX_DURATION_MS
    ),
    "network_ms": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=_MAX_DURATION_MS
    ),
    "materialization_ms": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=_MAX_DURATION_MS
    ),
    "time_to_visible_ms": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=_MAX_DURATION_MS
    ),
    "time_to_entry_ms": AttributeSpec(
        AttributeType.FLOAT, CardinalityClass.MEASUREMENT, minimum=0.0, maximum=_MAX_DURATION_MS
    ),
}

#: Which attribute keys each event name may carry (always includes the
#: common keys). Enforced both by :func:`sanitize_attributes` (drops
#: anything else) and by ``GameEvent`` (raises on anything else).
EVENT_ATTRIBUTE_ALLOWLIST: dict[GameEventName, frozenset[str]] = {
    GameEventName.FRONTIER_DISCOVERED: frozenset({"frontier_id", "depth", "exit_direction"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_QUEUED: frozenset({"frontier_id", "depth", "exit_direction"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_SENT: frozenset({"frontier_id", "depth", "exit_direction", "queue_ms"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_RESPONSE_RECEIVED: frozenset({"provider", "model", "network_ms"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_ACCEPTED: frozenset(
        {"provider", "model", "room_type", "room_size", "danger"}
    )
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_NORMALIZED: frozenset(
        {"provider", "model", "room_type", "room_size", "danger", "normalize_reason"}
    )
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_REJECTED: frozenset({"reject_reason", "provider", "model"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.GENERATION_FALLBACK_APPLIED: frozenset({"fallback_reason", "provider", "model"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.ROOM_COMMITTED: frozenset(
        {
            "room_id",
            "frontier_id",
            "room_type",
            "room_size",
            "danger",
            "exit_count",
            "has_secret",
            "enemy_density",
            "loot_density",
            "materialization_ms",
            "provider",
            "model",
        }
    )
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.DOOR_REVEALED: frozenset({"room_id", "time_to_visible_ms"})
    | COMMON_ATTRIBUTE_KEYS,
    GameEventName.ROOM_ENTERED: frozenset({"room_id", "time_to_entry_ms", "provider", "model"})
    | COMMON_ATTRIBUTE_KEYS,
}

#: Attribute keys that are id-shaped and therefore correlation-only: valid on
#: spans/logs, never as a metric dimension. Includes the two ``GameEvent``
#: top-level correlation fields alongside the id-shaped attribute keys.
CORRELATION_ONLY_KEYS = frozenset(
    key
    for key, spec in ATTRIBUTE_REGISTRY.items()
    if spec.cardinality is CardinalityClass.CORRELATION
) | {"run_id", "request_id"}

#: Attribute keys that are continuous numeric observations (durations,
#: densities): valid as a histogram's recorded value, never as a metric
#: dimension (a label a metric is grouped by).
MEASUREMENT_ONLY_KEYS = frozenset(
    key
    for key, spec in ATTRIBUTE_REGISTRY.items()
    if spec.cardinality is CardinalityClass.MEASUREMENT
)

#: Attribute keys safe to use as a metric dimension on a game-side metric.
GAME_METRIC_DIMENSIONS = frozenset(
    key for key, spec in ATTRIBUTE_REGISTRY.items() if spec.cardinality is CardinalityClass.LOW
)


def _validate_value(spec: AttributeSpec, value: JsonValue) -> JsonValue | None:
    """Return ``value`` if it satisfies ``spec``, else ``None`` (never raises)."""
    if spec.type is AttributeType.BOOL:
        return value if isinstance(value, bool) else None
    if spec.type is AttributeType.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if spec.minimum is not None and value < spec.minimum:
            return None
        if spec.maximum is not None and value > spec.maximum:
            return None
        return value
    if spec.type is AttributeType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        number = float(value)
        if spec.minimum is not None and number < spec.minimum:
            return None
        if spec.maximum is not None and number > spec.maximum:
            return None
        return number
    if spec.type is AttributeType.ENUM:
        return (
            value
            if isinstance(value, str) and value in (spec.allowed_values or frozenset())
            else None
        )
    if spec.type is AttributeType.ID:
        pattern = spec.pattern or _ID_RE
        return value if isinstance(value, str) and pattern.match(value) else None
    if spec.type is AttributeType.STRING:
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text or len(text) > MAX_ATTRIBUTE_STRING_LENGTH:
            return None
        if "\n" in text or "\r" in text:
            return None
        if _SECRET_LIKE_RE.search(text):
            return None
        if not _LABEL_RE.match(text):
            return None
        return text
    return None  # pragma: no cover - exhaustive over AttributeType


def sanitize_attributes(
    event_name: GameEventName, attributes: Mapping[str, JsonValue] | None
) -> dict[str, JsonValue]:
    """Return only the allowlisted, well-typed, in-range attributes for ``event_name``.

    Never raises. This is the safe entry point for attributes coming from an
    untrusted producer (the game client): a bridge should sanitize first,
    then build :class:`GameEvent` from the result, which then cannot fail
    the attribute allowlist check.
    """
    if not isinstance(attributes, Mapping):
        return {}
    allowed = EVENT_ATTRIBUTE_ALLOWLIST.get(event_name, COMMON_ATTRIBUTE_KEYS)
    result: dict[str, JsonValue] = {}
    for key, value in attributes.items():
        if len(result) >= MAX_ATTRIBUTES_PER_EVENT:
            break
        if key not in allowed:
            continue
        spec = ATTRIBUTE_REGISTRY.get(key)
        if spec is None:
            continue
        validated = _validate_value(spec, value)
        if validated is not None:
            result[key] = validated
    return result


def assert_safe_metric_dimensions(dimensions: Mapping[str, str]) -> None:
    """Raise ``ValueError`` if any key in ``dimensions`` is not a bounded metric dimension.

    Accepts the union of :data:`GAME_METRIC_DIMENSIONS` and the director's
    own :data:`~dungeon_director.telemetry.METRIC_DIMENSIONS`, so the same
    guard can check a game-side or a director-side dimension set. Rejects
    every correlation-only key and every measurement key, each with a
    distinct message, plus anything not recognized at all.
    """
    for key in dimensions:
        if key in CORRELATION_ONLY_KEYS:
            raise ValueError(
                f"{key!r} is a correlation-only field and must never be a metric dimension"
            )
        if key in MEASUREMENT_ONLY_KEYS:
            raise ValueError(
                f"{key!r} is a numeric measurement, not a label, and must never be a "
                "metric dimension"
            )
        if key not in GAME_METRIC_DIMENSIONS and key not in DIRECTOR_METRIC_DIMENSIONS:
            raise ValueError(f"{key!r} is not a recognized bounded metric dimension")


class GameEvent(BaseModel):
    """One game-reported lifecycle event for ``POST /v1/telemetry/game``.

    Strict by design: an unknown top-level field, an unknown ``event_name``,
    a malformed id, or an attribute outside this event's allowlist all raise
    ``ValidationError``. See :func:`sanitize_attributes` for the
    never-raises path used to prepare untrusted input before constructing
    this model.
    """

    model_config = ConfigDict(extra="forbid")

    event_name: GameEventName
    run_id: BoundedId
    request_id: BoundedId | None = None
    timestamp: AwareDatetime
    traceparent: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    attributes: Annotated[dict[str, JsonValue], Field(max_length=MAX_ATTRIBUTES_PER_EVENT)] = {}

    @field_validator("traceparent")
    @classmethod
    def _traceparent_shape(cls, value: str | None) -> str | None:
        if value is not None and not _TRACEPARENT_RE.match(value):
            raise ValueError("traceparent must match the W3C traceparent format")
        return value

    @model_validator(mode="after")
    def _request_id_required_when_expected(self) -> GameEvent:
        if self.request_id is None and self.event_name not in _EVENTS_WITHOUT_REQUEST_ID:
            raise ValueError(f"request_id is required for event_name={self.event_name.value!r}")
        return self

    @model_validator(mode="after")
    def _attributes_within_allowlist(self) -> GameEvent:
        sanitized = sanitize_attributes(self.event_name, self.attributes)
        if sanitized != self.attributes:
            raise ValueError(
                "attributes contain a key or value outside the allowlisted contract "
                f"for event_name={self.event_name.value!r}; see sanitize_attributes()"
            )
        return self


class GameEventBatch(BaseModel):
    """The ``POST /v1/telemetry/game`` request body."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    events: Annotated[list[GameEvent], Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)]
