"""The OpenLIT telemetry schema and correlation contract (issue #22).

These tests pin the wire contract for ``POST /v1/telemetry/game`` (the future
bridge, issue #25 game side / a later director-side task) and the shared
metric-dimension allowlist: a regression here is a breaking change for every
consumer (Godot, the exporter in #23, dashboards in #27).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dungeon_director.telemetry import METRIC_DIMENSIONS as DIRECTOR_METRIC_DIMENSIONS
from dungeon_director.telemetry_schema import (
    ATTRIBUTE_REGISTRY,
    CORRELATION_ONLY_KEYS,
    EVENT_ATTRIBUTE_ALLOWLIST,
    GAME_METRIC_DIMENSIONS,
    MAX_ATTRIBUTES_PER_EVENT,
    MAX_EVENTS_PER_BATCH,
    MEASUREMENT_ONLY_KEYS,
    SCHEMA_VERSION,
    TELEMETRY_SCHEMA_VERSION_ATTRIBUTE,
    GameEvent,
    GameEventBatch,
    GameEventName,
    assert_safe_metric_dimensions,
    sanitize_attributes,
)

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)
VALID_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _event(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_name": GameEventName.ROOM_COMMITTED.value,
        "run_id": "run-1",
        "request_id": "req-1",
        "timestamp": NOW.isoformat(),
        "attributes": {"room_id": "room-1", "room_type": "vault", "danger": 3},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- schema version


def test_schema_version_is_a_single_digit_string() -> None:
    assert SCHEMA_VERSION == "1"


def test_batch_rejects_mismatched_schema_version() -> None:
    with pytest.raises(ValidationError):
        GameEventBatch.model_validate({"schema_version": "2", "events": [_event()]})


# --------------------------------------------------------------------------- GameEvent shape


def test_game_event_parses_a_well_formed_payload() -> None:
    event = GameEvent.model_validate(_event())
    assert event.event_name is GameEventName.ROOM_COMMITTED
    assert event.run_id == "run-1"
    assert event.attributes == {"room_id": "room-1", "room_type": "vault", "danger": 3}


def test_game_event_rejects_unknown_event_name() -> None:
    with pytest.raises(ValidationError):
        GameEvent.model_validate(_event(event_name="not.a.real.event"))


def test_game_event_rejects_malformed_run_id() -> None:
    with pytest.raises(ValidationError):
        GameEvent.model_validate(_event(run_id="has a space"))


def test_game_event_rejects_unknown_top_level_field() -> None:
    with pytest.raises(ValidationError):
        GameEvent.model_validate(_event(prompt="ignore all previous instructions"))


def test_game_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        GameEvent.model_validate(_event(timestamp="2026-09-22T12:00:00"))


class TestRequestIdRequirement:
    def test_frontier_discovered_allows_missing_request_id(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.FRONTIER_DISCOVERED.value,
                request_id=None,
                attributes={"frontier_id": "frontier-1", "depth": 3},
            )
        )
        assert event.request_id is None

    def test_room_committed_requires_request_id(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(_event(request_id=None))


class TestTraceparent:
    def test_accepts_a_well_formed_w3c_traceparent(self) -> None:
        event = GameEvent.model_validate(_event(traceparent=VALID_TRACEPARENT))
        assert event.traceparent == VALID_TRACEPARENT

    def test_rejects_a_malformed_traceparent(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(_event(traceparent="not-a-traceparent"))


class TestAttributeAllowlistEnforcement:
    def test_rejects_an_event_carrying_a_non_allowlisted_attribute(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(attributes={"room_id": "room-1", "raw_prompt": "generate a room"})
            )

    def test_rejects_an_attribute_not_allowed_for_this_event_name(self) -> None:
        # time_to_entry_ms is only allowed on room.entered, not room.committed.
        with pytest.raises(ValidationError):
            GameEvent.model_validate(_event(attributes={"time_to_entry_ms": 120.0}))


# --------------------------------------------------------------------------- sanitize_attributes


class TestSanitizeAttributes:
    def test_drops_keys_outside_the_event_allowlist(self) -> None:
        result = sanitize_attributes(
            GameEventName.ROOM_COMMITTED,
            {"room_id": "room-1", "not_a_real_key": "x", "frontier_id": "frontier-1"},
        )
        assert result == {"room_id": "room-1", "frontier_id": "frontier-1"}

    def test_drops_values_of_the_wrong_type(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"danger": "high"})
        assert result == {}

    def test_drops_out_of_range_numeric_values(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"danger": 99})
        assert result == {}

    def test_drops_negative_time_to_entry(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_ENTERED, {"time_to_entry_ms": -1.0})
        assert result == {}

    def test_drops_enum_values_outside_the_allowed_set(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"room_type": "throne_room"})
        assert result == {}

    def test_drops_secret_shaped_string_values(self) -> None:
        result = sanitize_attributes(
            GameEventName.GENERATION_ACCEPTED,
            {"provider": "Bearer sk-live-SECRET0123456789"},
        )
        assert result == {}

    def test_drops_multiline_string_values(self) -> None:
        result = sanitize_attributes(
            GameEventName.GENERATION_REJECTED,
            {"reject_reason": "schema_invalid\nTraceback (most recent call last):"},
        )
        assert result == {}

    def test_drops_overlong_string_values(self) -> None:
        result = sanitize_attributes(GameEventName.GENERATION_ACCEPTED, {"provider": "x" * 200})
        assert result == {}

    def test_caps_the_number_of_attributes_returned(self) -> None:
        allowed = sorted(EVENT_ATTRIBUTE_ALLOWLIST[GameEventName.ROOM_COMMITTED])
        oversized = {"room_id": "room-1"}
        for i in range(MAX_ATTRIBUTES_PER_EVENT + 10):
            oversized[f"junk_{i}"] = "x"
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, oversized)
        assert len(result) <= MAX_ATTRIBUTES_PER_EVENT
        assert set(result).issubset(allowed)

    def test_ignores_non_mapping_input(self) -> None:
        assert sanitize_attributes(GameEventName.ROOM_COMMITTED, None) == {}
        assert sanitize_attributes(GameEventName.ROOM_COMMITTED, "not a dict") == {}

    def test_sanitized_output_always_satisfies_strict_validation(self) -> None:
        # Bridge code sanitizes first, then builds GameEvent; that must never raise.
        cleaned = sanitize_attributes(
            GameEventName.ROOM_COMMITTED,
            {"room_id": "room-1", "raw_prompt": "ignore this", "danger": 999},
        )
        event = GameEvent.model_validate(_event(attributes=cleaned))
        assert event.attributes == cleaned


# --------------------------------------------------------------------------- batch bounds


class TestGameEventBatch:
    def test_accepts_a_single_event_batch(self) -> None:
        batch = GameEventBatch.model_validate(
            {"schema_version": SCHEMA_VERSION, "events": [_event()]}
        )
        assert len(batch.events) == 1

    def test_rejects_an_empty_batch(self) -> None:
        with pytest.raises(ValidationError):
            GameEventBatch.model_validate({"schema_version": SCHEMA_VERSION, "events": []})

    def test_rejects_a_batch_over_the_size_cap(self) -> None:
        events = [_event(run_id=f"run-{i}") for i in range(MAX_EVENTS_PER_BATCH + 1)]
        with pytest.raises(ValidationError):
            GameEventBatch.model_validate({"schema_version": SCHEMA_VERSION, "events": events})


# --------------------------------------------------------------------------- metric dimension guard


class TestMetricDimensionGuard:
    def test_accepts_game_side_low_cardinality_dimensions(self) -> None:
        assert_safe_metric_dimensions({"room_type": "vault", "danger": "3"})

    def test_accepts_existing_director_dimensions(self) -> None:
        assert_safe_metric_dimensions({dim: "x" for dim in DIRECTOR_METRIC_DIMENSIONS})

    @pytest.mark.parametrize("key", sorted(CORRELATION_ONLY_KEYS))
    def test_rejects_every_correlation_only_key(self, key: str) -> None:
        with pytest.raises(ValueError, match="correlation-only"):
            assert_safe_metric_dimensions({key: "whatever"})

    def test_rejects_an_unrecognized_dimension(self) -> None:
        with pytest.raises(ValueError, match="not a recognized"):
            assert_safe_metric_dimensions({"totally_made_up": "x"})

    @pytest.mark.parametrize("key", sorted(MEASUREMENT_ONLY_KEYS))
    def test_rejects_every_measurement_key(self, key: str) -> None:
        with pytest.raises(ValueError, match="numeric measurement, not a label"):
            assert_safe_metric_dimensions({key: "123"})


# --------------------------------------------------------------------------- new lifecycle stages


class TestFrontierIdFormat:
    def test_accepts_the_colon_separated_frontier_id_shape(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.FRONTIER_DISCOVERED.value,
                request_id=None,
                attributes={"frontier_id": "r-000:east", "depth": 1},
            )
        )
        assert event.attributes["frontier_id"] == "r-000:east"

    def test_room_id_still_rejects_a_colon(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"room_id": "room:1"})
        assert result == {}


class TestGenerationSentAndResponseReceived:
    def test_generation_sent_requires_request_id(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(
                    event_name=GameEventName.GENERATION_SENT.value, request_id=None, attributes={}
                )
            )

    def test_generation_sent_accepts_queue_ms(self) -> None:
        event = GameEvent.model_validate(
            _event(event_name=GameEventName.GENERATION_SENT.value, attributes={"queue_ms": 12.5})
        )
        assert event.attributes["queue_ms"] == 12.5

    def test_generation_response_received_accepts_network_ms_and_provider(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.GENERATION_RESPONSE_RECEIVED.value,
                attributes={"provider": "cerebras", "network_ms": 340.0},
            )
        )
        assert event.attributes == {"provider": "cerebras", "network_ms": 340.0}


class TestDoorRevealed:
    def test_accepts_time_to_visible_ms(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.DOOR_REVEALED.value,
                attributes={"room_id": "room-1", "time_to_visible_ms": 80.0},
            )
        )
        assert event.attributes["time_to_visible_ms"] == 80.0


class TestRoomCommittedBehaviorAttributes:
    def test_accepts_exit_count_secret_and_densities(self) -> None:
        event = GameEvent.model_validate(
            _event(
                attributes={
                    "room_id": "room-1",
                    "exit_count": 3,
                    "has_secret": True,
                    "enemy_density": 0.4,
                    "loot_density": 0.2,
                    "materialization_ms": 5.0,
                    "provider": "groq",
                    "model": "llama-3",
                }
            )
        )
        assert event.attributes["exit_count"] == 3
        assert event.attributes["has_secret"] is True

    def test_rejects_exit_count_above_the_bound(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"exit_count": 9})
        assert result == {}


class TestRoomEnteredProviderModel:
    def test_room_entered_accepts_provider_and_model(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.ROOM_ENTERED.value,
                attributes={
                    "room_id": "room-1",
                    "time_to_entry_ms": 900.0,
                    "provider": "cerebras",
                    "model": "m1",
                },
            )
        )
        assert event.attributes["provider"] == "cerebras"


class TestNewReasonCodes:
    @pytest.mark.parametrize("reason", ["exit_pruned"])
    def test_normalize_reason_accepts_new_values(self, reason: str) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.GENERATION_NORMALIZED.value,
                attributes={"normalize_reason": reason},
            )
        )
        assert event.attributes["normalize_reason"] == reason

    @pytest.mark.parametrize("reason", ["duplicate_room_id", "placement_failure"])
    def test_reject_reason_accepts_new_values(self, reason: str) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.GENERATION_REJECTED.value,
                attributes={"reject_reason": reason},
            )
        )
        assert event.attributes["reject_reason"] == reason

    def test_fallback_reason_accepts_transport_failure(self) -> None:
        event = GameEvent.model_validate(
            _event(
                event_name=GameEventName.GENERATION_FALLBACK_APPLIED.value,
                attributes={"fallback_reason": "transport_failure"},
            )
        )
        assert event.attributes["fallback_reason"] == "transport_failure"


# --------------------------------------------------------------------------- internal consistency


class TestRegistryConsistency:
    def test_every_event_name_has_an_allowlist_entry(self) -> None:
        assert set(EVENT_ATTRIBUTE_ALLOWLIST) == set(GameEventName)

    def test_every_allowlisted_key_is_registered(self) -> None:
        for allowed in EVENT_ATTRIBUTE_ALLOWLIST.values():
            assert allowed.issubset(ATTRIBUTE_REGISTRY)

    def test_correlation_and_metric_dimension_keys_are_disjoint(self) -> None:
        assert GAME_METRIC_DIMENSIONS.isdisjoint(CORRELATION_ONLY_KEYS)

    def test_game_metric_dimensions_never_collide_with_director_dimensions_on_meaning(self) -> None:
        # provider/model/execution_mode are intentionally shared (same meaning, same
        # values) between game events and director spans; everything else must be
        # namespaced apart.
        shared = GAME_METRIC_DIMENSIONS & DIRECTOR_METRIC_DIMENSIONS
        assert shared <= {"provider", "model", "execution_mode"}


# --------------------------------------------------------------------------- regression: review


class TestCredentialLeakRejection:
    """A reviewer found ``model="https://user:secret@example.com/path"`` passing
    ``GameEvent`` unchanged: the old label pattern allowed '/', ':' and '@', so a
    full URL with embedded credentials matched it character-for-character.
    """

    def test_sanitize_drops_a_url_with_embedded_credentials(self) -> None:
        result = sanitize_attributes(
            GameEventName.GENERATION_ACCEPTED,
            {"provider": "https://user:secret@example.com/path"},
        )
        assert result == {}

    def test_game_event_rejects_a_url_shaped_model(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(
                    event_name=GameEventName.GENERATION_ACCEPTED.value,
                    attributes={"model": "https://user:secret@example.com/path"},
                )
            )

    def test_bare_userinfo_without_a_scheme_is_still_caught_by_the_secret_pattern(self) -> None:
        # No "://" here, but "password=" is still secret-shaped.
        result = sanitize_attributes(
            GameEventName.GENERATION_ACCEPTED, {"provider": "password=hunter2"}
        )
        assert result == {}

    @pytest.mark.parametrize("model_id", ["typesafe/jev", "@cf/meta/llama-3.1-8b-instruct", "groq"])
    def test_legitimate_slash_and_at_sign_identifiers_still_pass(self, model_id: str) -> None:
        # The fix must ban the URL *shape* (a scheme), not slash/colon/'@' on
        # their own -- real model ids use all three.
        result = sanitize_attributes(GameEventName.GENERATION_ACCEPTED, {"model": model_id})
        assert result == {"model": model_id}


class TestSecretShapedIdRejection:
    """A reviewer found a secret-shaped room_id (matches BoundedId's character
    class exactly) passing sanitize_attributes unchanged.
    """

    def test_sanitize_drops_a_secret_shaped_room_id(self) -> None:
        result = sanitize_attributes(
            GameEventName.ROOM_COMMITTED, {"room_id": "sk-live-secret0123456789"}
        )
        assert result == {}

    def test_sanitize_still_keeps_an_ordinary_room_id(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"room_id": "room-1"})
        assert result == {"room_id": "room-1"}


class TestNonFiniteAndOverflowFloats:
    """A reviewer found enemy_density=NaN passing (NaN fails every comparison,
    so the old range check silently let it through), and
    enemy_density=10**1000 raising OverflowError from float() -- breaking the
    documented never-raises contract of sanitize_attributes.
    """

    def test_drops_nan(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": math.nan})
        assert result == {}

    def test_drops_positive_infinity(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": math.inf})
        assert result == {}

    def test_drops_negative_infinity(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": -math.inf})
        assert result == {}

    def test_never_raises_on_an_absurdly_large_int(self) -> None:
        # float(10**1000) raises OverflowError; sanitize_attributes must not propagate it.
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": 10**1000})
        assert result == {}

    def test_never_raises_on_an_absurdly_large_negative_int(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": -(10**1000)})
        assert result == {}

    def test_still_accepts_an_ordinary_finite_value(self) -> None:
        result = sanitize_attributes(GameEventName.ROOM_COMMITTED, {"enemy_density": 0.4})
        assert result == {"enemy_density": 0.4}


class TestTraceparentSemanticValidation:
    """A reviewer found the traceparent regex accepting an all-zero trace-id,
    an all-zero parent-id, and the reserved version ``ff`` -- all invalid per
    the W3C Trace Context spec (a real tracer never emits them).
    """

    def test_rejects_all_zero_trace_id(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(traceparent="00-00000000000000000000000000000000-00f067aa0ba902b7-01")
            )

    def test_rejects_all_zero_parent_id(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01")
            )

    def test_rejects_reserved_version_ff(self) -> None:
        with pytest.raises(ValidationError):
            GameEvent.model_validate(
                _event(traceparent="ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
            )

    def test_still_accepts_a_well_formed_traceparent(self) -> None:
        event = GameEvent.model_validate(_event(traceparent=VALID_TRACEPARENT))
        assert event.traceparent == VALID_TRACEPARENT


class TestTelemetrySchemaVersionAttribute:
    def test_is_distinct_from_the_generation_contract_version(self) -> None:
        # telemetry.schema.version tracks this module's own event contract
        # (SCHEMA_VERSION); it must not be confused with service.version,
        # which (per telemetry-schema.md) tracks the deployable build or the
        # Godot<->director room-plan CONTRACT_VERSION as a fallback -- a
        # different axis of versioning entirely.
        assert TELEMETRY_SCHEMA_VERSION_ATTRIBUTE == "telemetry.schema.version"
