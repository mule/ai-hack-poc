"""Tests for the canonical dungeon director contracts (issue #3).

Run from the repository root or the ``director/`` directory:

    python -m pytest director/tests/test_contracts.py

Only pydantic v2 and pytest are required; FastAPI is not needed because the
contracts are pure data models.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

DIRECTOR_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DIRECTOR_DIR.parent
for entry in (str(DIRECTOR_DIR), str(REPO_ROOT / "contracts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

# The dungeon_director import must follow the sys.path setup above.
from dungeon_director import contracts as C  # noqa: E402

FIXTURES_DIR = Path(os.environ.get("DUNGEON_CONTRACTS_DIR", REPO_ROOT / "contracts" / "fixtures"))
SCHEMAS_DIR = REPO_ROOT / "contracts" / "schemas"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fixture acceptance (issue acceptance criteria: JSON examples validate)
# ---------------------------------------------------------------------------


class TestFixturesValidate:
    def test_generation_request_fixture(self) -> None:
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        assert request.state.depth == 3
        assert request.state.unresolved_exits[0].direction is C.ExitDirection.NORTH
        assert request.options is not None and request.options.max_danger == 3
        assert request.target_exit is not None
        assert request.target_exit in request.state.unresolved_exits

    def test_generation_response_fixture(self) -> None:
        response = C.GenerationResponse.model_validate(load_fixture("generation_response.json"))
        assert response.success is True
        assert response.room is not None
        assert response.room.room_type is C.RoomType.ROOM
        assert response.metadata.provider == "rules-baseline"
        assert response.metadata.error is None

    def test_generation_response_failure_fixture(self) -> None:
        response = C.GenerationResponse.model_validate(
            load_fixture("generation_response_failure.json")
        )
        assert response.success is False
        assert response.room is None
        assert response.metadata.error is not None
        assert response.metadata.error.code is C.ErrorKind.SCHEMA_VIOLATION
        assert response.metadata.error.raw_excerpt

    def test_malformed_provider_response_rejected(self) -> None:
        """Documented failure path: raw provider output fails RoomPlan validation."""
        raw = load_fixture("malformed_provider_response.json")
        with pytest.raises(ValidationError) as excinfo:
            C.RoomPlan.model_validate(raw)
        errors = excinfo.value.errors()
        fields = {e["loc"][0] for e in errors if e["type"] != "extra_forbidden"}
        assert "room_type" in fields  # unknown enum value
        assert "danger" in fields  # out of range
        assert "enemy_density" in fields  # wrong type / range
        assert any(e["type"] == "extra_forbidden" for e in errors)  # tile geometry leaked

    def test_malformed_fixture_produces_failure_envelope(self) -> None:
        raw_text = (FIXTURES_DIR / "malformed_provider_response.json").read_text("utf-8")
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        try:
            C.RoomPlan.model_validate(json.loads(raw_text))
            raised = False
        except ValidationError as exc:
            raised = True
            message = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
        assert raised
        response = C.GenerationResponse.failure(
            request_id=request.request_id,
            run_id=request.run_id,
            provider="groq",
            model="gpt-oss-20b",
            code=C.ErrorKind.SCHEMA_VIOLATION,
            message=f"Provider output failed RoomPlan validation: {message}",
            raw_excerpt=raw_text[:4096],
        )
        assert response.success is False
        assert response.room is None
        # The failure envelope itself must round-trip through JSON.
        revived = C.GenerationResponse.model_validate(json.loads(response.model_dump_json()))
        assert revived.metadata.error is not None
        assert revived.metadata.error.code is C.ErrorKind.SCHEMA_VIOLATION


# ---------------------------------------------------------------------------
# Committed JSON Schemas stay in sync with the models
# ---------------------------------------------------------------------------


class TestSchemaExport:
    def test_exported_schemas_match_models(self) -> None:
        import export_schemas

        assert SCHEMAS_DIR.is_dir(), "contracts/schemas/ has not been generated"
        for name, model in export_schemas.SCHEMAS.items():
            committed = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text("utf-8"))
            assert committed == export_schemas.build_schema(name, model), (
                f"{name}.schema.json is stale; re-run contracts/export_schemas.py"
            )

    def test_schema_carries_explicit_version(self) -> None:
        schema = json.loads((SCHEMAS_DIR / "room_plan.schema.json").read_text("utf-8"))
        assert schema["x-contract-version"] == C.CONTRACT_VERSION
        assert schema["$id"].endswith("room_plan.schema.json")


# ---------------------------------------------------------------------------
# Versioning (acceptance: explicit version, forward-compatible policy)
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_contract_version_required_on_input(self) -> None:
        payload = {
            "request_id": "req-1",
            "run_id": "run-1",
            "state": {"depth": 1, "player": {"hp": 10, "max_hp": 10}},
        }
        with pytest.raises(ValidationError) as excinfo:
            C.GenerationRequest.model_validate(payload)
        assert any(
            e["type"] == "missing" and "contract_version" in e["loc"]
            for e in excinfo.value.errors()
        )

    def test_patch_and_minor_bumps_accepted(self) -> None:
        for version in ("1.0.1", "1.4.0"):
            C.GenerationRequest(
                contract_version=version,
                request_id="req-1",
                run_id="run-1",
                state={
                    "depth": 1,
                    "player": {"hp": 10, "max_hp": 10},
                    "unresolved_exits": [{"room_id": "r-1", "direction": "north"}],
                },
                target_exit={"room_id": "r-1", "direction": "north"},
            )

    def test_major_bump_rejected_with_stable_error_type(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            C.GenerationRequest(
                contract_version="2.0.0",
                request_id="req-1",
                run_id="run-1",
                state={"depth": 1, "player": {"hp": 10, "max_hp": 10}},
            )
        assert any(e["type"] == "unsupported_contract_version" for e in excinfo.value.errors())

    def test_non_semver_version_rejected_with_stable_error_type(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            C.GenerationResponse.model_validate(
                {
                    "contract_version": "one",
                    "request_id": "req-1",
                    "run_id": "run-1",
                    "room": None,
                    "metadata": {
                        "provider": "p",
                        "model": "m",
                        "started_at": "2026-09-19T10:00:00Z",
                        "completed_at": "2026-09-19T10:00:01Z",
                    },
                }
            )
        assert any(e["type"] == "unsupported_contract_version" for e in excinfo.value.errors())


# ---------------------------------------------------------------------------
# Strictness and bounds
# ---------------------------------------------------------------------------


def make_room(**overrides: object) -> dict:
    room: dict = {
        "room_id": "r-100",
        "depth": 2,
        "room_type": "cavern",
        "size": "medium",
        "danger": 3,
    }
    room.update(overrides)
    return room


def make_response(**overrides: object) -> dict:
    payload: dict = {
        "contract_version": C.CONTRACT_VERSION,
        "request_id": "req-1",
        "run_id": "run-1",
        "room": make_room(),
        "metadata": {
            "provider": "rules-baseline",
            "model": "builtin-v1",
            "started_at": "2026-09-19T10:00:00Z",
            "completed_at": "2026-09-19T10:00:01Z",
        },
    }
    payload.update(overrides)
    return payload


class TestStrictness:
    def test_unknown_top_level_field_rejected(self) -> None:
        payload = make_response(certainty=0.9)  # provider leakage attempt
        with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
            C.GenerationResponse.model_validate(payload)

    def test_unknown_nested_field_rejected(self) -> None:
        room = make_room(tile_grid=[[1, 1], [1, 1]])  # geometry leakage attempt
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(room)

    def test_densities_are_bounded(self) -> None:
        for field in ("enemy_density", "loot_density", "secret_probability"):
            with pytest.raises(ValidationError):
                C.RoomPlan.model_validate(make_room(**{field: 1.5}))
            with pytest.raises(ValidationError):
                C.RoomPlan.model_validate(make_room(**{field: -0.1}))

    def test_danger_bounded(self) -> None:
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(danger=0))
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(danger=6))

    def test_depth_bounded(self) -> None:
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(depth=0))
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(depth=10_000))

    def test_exit_list_bounded(self) -> None:
        exits = [{"direction": d} for d in ("north", "south", "east", "west", "up", "down")]
        exits = exits * 2  # 12 > 8
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(exits=exits))

    def test_bad_room_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            C.RoomPlan.model_validate(make_room(room_id="bad id!"))

    def test_hp_cannot_exceed_max_hp(self) -> None:
        with pytest.raises(ValidationError):
            C.PlayerState.model_validate({"hp": 11, "max_hp": 10})

    def test_unbounded_lists_rejected(self) -> None:
        events = [{"event": "e"} for _ in range(40)]  # > 32
        with pytest.raises(ValidationError):
            C.DungeonState.model_validate(
                {"depth": 1, "player": {"hp": 1, "max_hp": 1}, "recent_events": events}
            )

    def test_provider_metadata_bounded(self) -> None:
        blob = {f"k{i}": i for i in range(33)}  # > 32 keys
        with pytest.raises(ValidationError):
            C.ResponseMetadata.model_validate(
                {
                    "provider": "p",
                    "model": "m",
                    "started_at": "2026-09-19T10:00:00Z",
                    "completed_at": "2026-09-19T10:00:01Z",
                    "provider_metadata": blob,
                }
            )

    def test_provider_metadata_serialized_byte_cap(self) -> None:
        def metadata_with_blob(blob: str) -> dict:
            return {
                "provider": "p",
                "model": "m",
                "started_at": "2026-09-19T10:00:00Z",
                "completed_at": "2026-09-19T10:00:01Z",
                "provider_metadata": {"blob": blob},
            }

        ok_len = C.MAX_PROVIDER_METADATA_JSON_BYTES - 32  # leaves room for JSON framing
        C.ResponseMetadata.model_validate(metadata_with_blob("x" * ok_len))
        with pytest.raises(ValidationError, match="8192"):
            C.ResponseMetadata.model_validate(metadata_with_blob("x" * (ok_len + 64)))

    def test_completed_at_before_started_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            C.ResponseMetadata.model_validate(
                {
                    "provider": "p",
                    "model": "m",
                    "started_at": "2026-09-19T10:00:01Z",
                    "completed_at": "2026-09-19T10:00:00Z",
                }
            )


# ---------------------------------------------------------------------------
# Success/failure invariant (documented failure path)
# ---------------------------------------------------------------------------


class TestSuccessInvariant:
    def test_success_without_room_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must carry a room"):
            C.GenerationResponse.model_validate(make_response(room=None))

    def test_failure_with_room_rejected(self) -> None:
        error = {"code": "provider_error", "message": "boom"}
        payload = make_response(
            success=False,
            room=make_room(),
            metadata={
                "provider": "p",
                "model": "m",
                "started_at": "2026-09-19T10:00:00Z",
                "completed_at": "2026-09-19T10:00:01Z",
                "error": error,
            },
        )
        with pytest.raises(ValidationError, match="must not carry a room"):
            C.GenerationResponse.model_validate(payload)

    def test_failure_without_error_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must carry error detail"):
            C.GenerationResponse.model_validate(
                make_response(
                    success=False,
                    room=None,
                    metadata={
                        "provider": "p",
                        "model": "m",
                        "started_at": "2026-09-19T10:00:00Z",
                        "completed_at": "2026-09-19T10:00:01Z",
                    },
                )
            )

    def test_success_with_error_rejected(self) -> None:
        error = {"code": "provider_error", "message": "stale"}
        with pytest.raises(ValidationError, match="must not carry error"):
            C.GenerationResponse.model_validate(
                make_response(
                    metadata={
                        "provider": "p",
                        "model": "m",
                        "started_at": "2026-09-19T10:00:00Z",
                        "completed_at": "2026-09-19T10:00:01Z",
                        "error": error,
                    }
                )
            )

    def test_failure_factory_truncates_oversized_excerpt(self) -> None:
        """failure() itself truncates raw provider output to 4096 chars."""
        response = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code=C.ErrorKind.SCHEMA_VIOLATION,
            message="bad",
            raw_excerpt="x" * 6000,
        )
        assert response.metadata.error is not None
        assert len(response.metadata.error.raw_excerpt or "") == 4096


# ---------------------------------------------------------------------------
# Timezone-aware timestamps
# ---------------------------------------------------------------------------


def make_metadata(**overrides: object) -> dict:
    payload: dict = {
        "provider": "p",
        "model": "m",
        "started_at": "2026-09-19T10:00:00Z",
        "completed_at": "2026-09-19T10:00:01Z",
    }
    payload.update(overrides)
    return payload


class TestTimezoneAwareTimestamps:
    def test_naive_started_at_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            C.ResponseMetadata.model_validate(make_metadata(started_at="2026-09-19T10:00:00"))
        assert any(e["type"] == "timezone_aware" for e in excinfo.value.errors())

    def test_naive_completed_at_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            C.ResponseMetadata.model_validate(make_metadata(completed_at="2026-09-19T10:00:01"))
        assert any(e["type"] == "timezone_aware" for e in excinfo.value.errors())

    def test_mixed_aware_and_naive_rejected(self) -> None:
        with pytest.raises(ValidationError):
            C.ResponseMetadata.model_validate(
                make_metadata(
                    started_at="2026-09-19T10:00:00+02:00",
                    completed_at="2026-09-19T10:00:01",
                )
            )

    def test_naive_error_occurred_at_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            C.ErrorDetail.model_validate(
                {
                    "code": "provider_error",
                    "message": "boom",
                    "occurred_at": "2026-09-19T10:00:01",
                }
            )
        assert any(e["type"] == "timezone_aware" for e in excinfo.value.errors())

    def test_aware_offsets_accepted(self) -> None:
        for offset in ("Z", "+00:00", "+02:00", "-05:30"):
            C.ResponseMetadata.model_validate(
                make_metadata(
                    started_at=f"2026-09-19T10:00:00{offset}",
                    completed_at=f"2026-09-19T10:00:01{offset}",
                )
            )


# ---------------------------------------------------------------------------
# Failure factory robustness for arbitrary provider errors
# ---------------------------------------------------------------------------


class TestFailureFactorySanitization:
    def test_garbage_inputs_never_raise(self) -> None:
        response = C.GenerationResponse.failure(
            request_id=None,
            run_id="bad id!",
            provider="",
            model=None,
            code="totally-unknown-code",
            message="   ",
            raw_excerpt=b"\xff\xfe not text" * 3000,
        )
        assert response.success is False
        assert response.room is None

    def test_invalid_ids_become_documented_sentinels(self) -> None:
        response = C.GenerationResponse.failure(
            request_id="bad id!",
            run_id=None,
            provider="p",
            model="m",
            code="provider_error",
            message="boom",
        )
        assert response.request_id == C.UNKNOWN_REQUEST_ID == "unknown-request-id"
        assert response.run_id == C.UNKNOWN_RUN_ID == "unknown-run-id"

    def test_provider_model_sanitized(self) -> None:
        response = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="   ",
            model="x" * 500,
            code="provider_error",
            message="boom",
        )
        assert response.metadata.provider == C.UNKNOWN_PROVIDER == "unknown"
        assert len(response.metadata.model) == 128

    def test_message_fallback_and_truncation(self) -> None:
        fallback = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code=C.ErrorKind.RATE_LIMITED,
            message="",
        )
        assert fallback.metadata.error is not None
        assert fallback.metadata.error.message == "Generation failed (rate_limited)."
        truncated = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code="provider_error",
            message="x" * 600,
        )
        assert truncated.metadata.error is not None
        assert len(truncated.metadata.error.message) == 500

    def test_unknown_code_falls_back_to_provider_error(self) -> None:
        response = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code="not-a-real-code",
            message="boom",
        )
        assert response.metadata.error is not None
        assert response.metadata.error.code is C.ErrorKind.PROVIDER_ERROR

    def test_naive_timestamps_normalized_and_completed_clamped(self) -> None:
        naive_start = datetime(2026, 9, 19, 10, 0, 0)
        naive_earlier = datetime(2026, 9, 19, 9, 0, 0)
        response = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code="provider_error",
            message="boom",
            started_at=naive_start,
            completed_at=naive_earlier,
        )
        assert response.metadata.started_at.tzinfo is not None
        assert response.metadata.completed_at.tzinfo is not None
        assert response.metadata.completed_at == response.metadata.started_at
        assert response.metadata.error is not None
        assert response.metadata.error.occurred_at.tzinfo is not None

    def test_oversized_provider_metadata_dropped(self) -> None:
        response = C.GenerationResponse.failure(
            request_id="req-1",
            run_id="run-1",
            provider="p",
            model="m",
            code="provider_error",
            message="boom",
            provider_metadata={"blob": "x" * 9000},
        )
        assert response.metadata.provider_metadata == {}


# ---------------------------------------------------------------------------
# RoomPlan / request semantic invariants
# ---------------------------------------------------------------------------


class TestSemanticInvariants:
    def test_duplicate_exit_directions_rejected(self) -> None:
        exits = [{"direction": "north"}, {"direction": "north", "kind": "secret"}]
        with pytest.raises(ValidationError, match="unique directions"):
            C.RoomPlan.model_validate(make_room(exits=exits))

    def test_distinct_exit_directions_accepted(self) -> None:
        exits = [{"direction": "north"}, {"direction": "south"}]
        C.RoomPlan.model_validate(make_room(exits=exits))

    def test_has_secret_with_zero_probability_rejected(self) -> None:
        with pytest.raises(ValidationError, match="secret_probability"):
            C.RoomPlan.model_validate(make_room(has_secret=True, secret_probability=0.0))

    def test_has_secret_with_positive_probability_accepted(self) -> None:
        C.RoomPlan.model_validate(make_room(has_secret=True, secret_probability=0.25))

    def test_success_from_request_rejects_depth_mismatch(self) -> None:
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        room = C.RoomPlan.model_validate(load_fixture("generation_response.json")["room"])
        assert room.depth != 4
        with pytest.raises(ValueError, match="state.depth"):
            C.GenerationResponse.success_from_request(
                request,
                room=room.model_copy(update={"depth": 4}),
                provider="rules-baseline",
                model="builtin-v1",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )

    def test_success_from_request_rejects_naive_timestamps_via_validation(self) -> None:
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        room = C.RoomPlan.model_validate(load_fixture("generation_response.json")["room"])
        naive = datetime(2026, 9, 19, 10, 0, 0)
        with pytest.raises(ValidationError):
            C.GenerationResponse.success_from_request(
                request,
                room=room,
                provider="rules-baseline",
                model="builtin-v1",
                started_at=naive,
                completed_at=naive,
            )


class TestTargetExit:
    def test_matching_target_exit_accepted(self) -> None:
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        assert request.target_exit == request.state.unresolved_exits[0]

    def test_absent_target_exit_rejected(self) -> None:
        payload = load_fixture("generation_request.json")
        del payload["target_exit"]
        with pytest.raises(ValidationError) as excinfo:
            C.GenerationRequest.model_validate(payload)
        assert any(
            e["type"] == "missing" and "target_exit" in e["loc"] for e in excinfo.value.errors()
        )

    def test_mismatched_target_exit_rejected(self) -> None:
        for mutation in (
            {"since_turn": 999},  # differs from frontier entry
            {"direction": "south"},
            {"room_id": "r-999"},
        ):
            payload = load_fixture("generation_request.json")
            payload["target_exit"] = {**payload["target_exit"], **mutation}
            with pytest.raises(ValidationError, match="unresolved_exits"):
                C.GenerationRequest.model_validate(payload)

    def test_target_exit_without_matching_frontier_entry_rejected(self) -> None:
        payload = load_fixture("generation_request.json")
        payload["state"]["unresolved_exits"] = []
        with pytest.raises(ValidationError, match="unresolved_exits"):
            C.GenerationRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------


class TestRoundTrips:
    @pytest.mark.parametrize(
        ("model", "fixture"),
        [
            (C.GenerationRequest, "generation_request.json"),
            (C.GenerationResponse, "generation_response.json"),
            (C.GenerationResponse, "generation_response_failure.json"),
            (C.DungeonState, None),
            (C.RoomPlan, None),
        ],
    )
    def test_round_trip(self, model: type[C.BaseModel], fixture: str | None) -> None:
        if fixture is not None:
            data = load_fixture(fixture)
        elif model is C.DungeonState:
            data = load_fixture("generation_request.json")["state"]
        else:
            data = load_fixture("generation_response.json")["room"]
        instance = model.model_validate(data)
        revived = model.model_validate(json.loads(instance.model_dump_json()))
        assert revived == instance

    def test_json_uses_enum_values_and_iso_dates(self) -> None:
        response = C.GenerationResponse.model_validate(load_fixture("generation_response.json"))
        dumped = json.loads(response.model_dump_json())
        assert dumped["room"]["room_type"] == "room"
        assert dumped["metadata"]["started_at"].startswith("2026-09-19T10:00:00")

    def test_success_from_request_helper(self) -> None:
        request = C.GenerationRequest.model_validate(load_fixture("generation_request.json"))
        started = datetime.now(UTC)
        response = C.GenerationResponse.success_from_request(
            request,
            room=C.RoomPlan.model_validate(load_fixture("generation_response.json")["room"]),
            provider="rules-baseline",
            model="builtin-v1",
            started_at=started,
            completed_at=started,
        )
        assert response.success and response.room is not None
        assert response.request_id == request.request_id
        assert response.run_id == request.run_id
