"""DirectorService: selection, timeout, cancellation and failure conversion."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from fakes import (
    BlockingProvider,
    BrokenAvailabilityProvider,
    ClassifiedFailureProvider,
    FakeProvider,
    MalformedProvider,
    RaisingProvider,
    RawResultProvider,
    SlowProvider,
    SuppressingProvider,
    make_request,
    valid_room_dict,
)

from dungeon_director.contracts import (
    CONTRACT_VERSION,
    ErrorKind,
    GenerationResponse,
    RoomPlan,
    UsageStats,
)
from dungeon_director.errors import DirectorConfigError, ProviderError
from dungeon_director.providers import DungeonDirectorProvider, ProviderResult
from dungeon_director.registry import ProviderRegistry, default_registry
from dungeon_director.service import DirectorService, status_for_error
from dungeon_director.settings import DirectorSettings


def make_service(
    *providers: DungeonDirectorProvider,
    default: str | None = None,
    default_model: str | None = None,
    timeout: float = 1.0,
) -> DirectorService:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    settings = DirectorSettings(
        default_provider=default or providers[0].provider_id,
        default_model=default_model,
        timeout_seconds=timeout,
    )
    return DirectorService(registry, settings)


def generate(service: DirectorService, request=None, **selection):
    return asyncio.run(service.generate(request or make_request(), **selection))


def assert_canonical_failure(outcome, *, code: ErrorKind, status: int):
    response = outcome.response
    assert outcome.status_code == status
    assert response.success is False
    assert response.room is None
    assert response.metadata.error is not None
    assert response.metadata.error.code is code
    # Round-trips through the contract, i.e. is a well-formed envelope.
    GenerationResponse.model_validate(response.model_dump(mode="json"))


# --- success and selection ---------------------------------------------------


def test_default_rules_provider_returns_a_valid_success_envelope():
    service = DirectorService(default_registry(), DirectorSettings())
    request = make_request()

    outcome = generate(service, request)

    response = outcome.response
    assert outcome.status_code == 200
    assert response.success is True
    assert response.contract_version == CONTRACT_VERSION
    assert (response.request_id, response.run_id) == (request.request_id, request.run_id)
    assert response.metadata.provider == "rules-baseline"
    assert response.metadata.model == "builtin-v1"
    assert response.room is not None and response.room.depth == request.state.depth
    assert response.metadata.usage == UsageStats(
        input_tokens=0, output_tokens=0, estimated_cost_usd=0.0
    )


def test_success_records_latency_and_ordered_timestamps():
    outcome = generate(make_service(FakeProvider("alpha")))

    metadata = outcome.response.metadata
    assert metadata.latency_ms is not None and metadata.latency_ms >= 0
    assert metadata.completed_at >= metadata.started_at
    assert metadata.started_at.utcoffset() is not None


def test_request_can_select_provider_and_model_explicitly():
    default, other = FakeProvider("alpha"), FakeProvider("beta", models=("m1", "m2"))
    service = make_service(default, other)

    outcome = generate(service, provider="beta", model="m2")

    assert outcome.response.success is True
    assert (outcome.response.metadata.provider, outcome.response.metadata.model) == ("beta", "m2")
    assert (default.calls, other.calls, other.models_seen) == (0, 1, ["m2"])


def test_no_selection_uses_the_configured_default_provider():
    first, second = FakeProvider("alpha"), FakeProvider("beta")
    service = make_service(first, second, default="beta")

    outcome = generate(service)

    assert outcome.response.metadata.provider == "beta"
    assert (first.calls, second.calls) == (0, 1)


def test_default_model_applies_to_the_default_provider_only():
    alpha = FakeProvider("alpha", models=("a1", "a2"))
    beta = FakeProvider("beta", models=("b1", "b2"))
    service = make_service(alpha, beta, default="alpha", default_model="a2")

    assert generate(service).response.metadata.model == "a2"
    assert generate(service, provider="beta").response.metadata.model == "b1"


def test_provider_usage_and_metadata_pass_through_to_the_response():
    class Reporting(FakeProvider):
        async def decide(self, request):
            return ProviderResult(
                payload=valid_room_dict(request),
                usage=UsageStats(input_tokens=10, output_tokens=5, estimated_cost_usd=0.001),
                provider_metadata={"finish_reason": "stop"},
            )

    outcome = generate(make_service(Reporting("alpha")))

    assert outcome.response.metadata.usage == UsageStats(
        input_tokens=10, output_tokens=5, estimated_cost_usd=0.001
    )
    assert outcome.response.metadata.provider_metadata == {"finish_reason": "stop"}


def test_unusable_default_provider_fails_at_startup_not_per_request():
    with pytest.raises(DirectorConfigError, match="default provider"):
        make_service(FakeProvider("alpha", available=False))
    with pytest.raises(DirectorConfigError, match="default provider"):
        make_service(FakeProvider("alpha"), default="missing")
    with pytest.raises(DirectorConfigError, match="default provider"):
        make_service(FakeProvider("alpha"), default_model="missing")


def test_unknown_provider_is_a_404_canonical_failure_and_never_calls_a_provider():
    provider = FakeProvider("alpha")

    outcome = generate(make_service(provider), provider="nope")

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=404)
    assert "nope" in outcome.response.metadata.error.message
    assert outcome.response.metadata.provider_metadata == {"selection_error": "unknown_provider"}
    assert provider.calls == 0


def test_unavailable_provider_is_a_503_canonical_failure_and_is_not_called():
    down = FakeProvider("beta", available=False)

    outcome = generate(make_service(FakeProvider("alpha"), down), provider="beta")

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=503)
    assert outcome.response.metadata.provider_metadata == {
        "selection_error": "provider_unavailable"
    }
    assert outcome.response.metadata.provider == "beta"
    assert down.calls == 0


def test_unknown_model_is_a_404_canonical_failure():
    provider = FakeProvider("alpha")

    outcome = generate(make_service(provider), provider="alpha", model="gpt-9")

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=404)
    assert outcome.response.metadata.provider_metadata == {"selection_error": "unknown_model"}
    assert provider.calls == 0


# --- timeout, cancellation, no retries --------------------------------------


def test_slow_provider_times_out_with_504_and_is_cancelled_without_retry():
    slow = SlowProvider()
    service = make_service(slow, timeout=0.05)

    started = time.perf_counter()
    outcome = generate(service)
    elapsed = time.perf_counter() - started

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_TIMEOUT, status=504)
    assert slow.cancelled is True, "the timed-out provider call must be cancelled, not abandoned"
    assert slow.calls == 1, "a timeout must not trigger a hidden retry"
    assert elapsed < 2, f"timeout should fire near 0.05s, took {elapsed:.2f}s"
    assert outcome.response.metadata.provider == "slow"
    assert outcome.response.metadata.latency_ms is not None
    assert outcome.response.metadata.latency_ms >= 40


def test_provider_that_raises_its_own_timeout_is_reported_as_a_provider_side_timeout():
    outcome = generate(
        make_service(RaisingProvider(TimeoutError("upstream took too long")), timeout=5)
    )

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_TIMEOUT, status=504)
    error = outcome.response.metadata.error
    assert "upstream took too long" not in error.message, "adapter text stays private"
    assert "5 seconds" not in error.message, "the director deadline did not expire"
    assert outcome.response.metadata.provider_metadata == {"timeout_origin": "provider"}


def test_director_deadline_expiry_is_labelled_and_states_the_deadline():
    outcome = generate(make_service(SlowProvider(), timeout=0.05))

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_TIMEOUT, status=504)
    assert "0.05 seconds" in outcome.response.metadata.error.message
    assert outcome.response.metadata.provider_metadata == {"timeout_origin": "director_deadline"}


def test_provider_that_swallows_cancellation_and_returns_late_is_not_a_success():
    stubborn = SuppressingProvider()

    outcome = generate(make_service(stubborn, timeout=0.05))

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_TIMEOUT, status=504)
    assert stubborn.saw_cancel is True and stubborn.calls == 1
    assert outcome.response.room is None
    assert outcome.response.metadata.provider_metadata == {"timeout_origin": "director_deadline"}


def test_provider_that_blocks_the_event_loop_past_the_deadline_is_not_a_success():
    """A synchronous sleep cannot be interrupted, but its late answer is discarded."""
    blocker = BlockingProvider(0.2)

    outcome = generate(make_service(blocker, timeout=0.05))

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_TIMEOUT, status=504)
    assert outcome.response.room is None
    assert outcome.response.metadata.latency_ms >= 150


def test_provider_that_answers_inside_the_deadline_is_still_a_success():
    outcome = generate(make_service(BlockingProvider(0.01), timeout=5))

    assert outcome.response.success is True


def test_cancelling_the_request_cancels_the_provider_and_propagates():
    async def scenario():
        slow = SlowProvider()
        service = make_service(slow, timeout=30)
        task = asyncio.create_task(service.generate(make_request()))
        await asyncio.wait_for(slow.started.wait(), timeout=2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return slow

    slow = asyncio.run(scenario())

    assert slow.cancelled is True
    assert slow.calls == 1


# --- provider failures -------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorKind.PROVIDER_ERROR, 502),
        (ErrorKind.RATE_LIMITED, 429),
        (ErrorKind.BUDGET_EXCEEDED, 429),
        (ErrorKind.SAFETY_REFUSAL, 502),
        (ErrorKind.PROVIDER_TIMEOUT, 504),
        (ErrorKind.INTERNAL_ERROR, 500),
    ],
)
def test_classified_provider_errors_keep_their_code_and_map_to_http_status(code, status):
    provider = ClassifiedFailureProvider(code, "upstream said no")

    outcome = generate(make_service(provider))

    assert_canonical_failure(outcome, code=code, status=status)
    assert outcome.response.metadata.provider == "classified"


SECRET = "sk-super-secret"
LEAKY_MESSAGE = f"upstream key {SECRET} failed"
LEAKY_EXCERPT = f"Authorization: Bearer {SECRET}"


@pytest.mark.parametrize("code", list(ErrorKind))
def test_adapter_supplied_error_text_never_reaches_the_response(code):
    provider = RaisingProvider(
        ProviderError(code, LEAKY_MESSAGE, raw_excerpt=LEAKY_EXCERPT), "leaky"
    )

    outcome = generate(make_service(provider))

    response = outcome.response
    body = response.model_dump_json()
    assert SECRET not in body and "Bearer" not in body and "upstream key" not in body
    assert response.metadata.error.raw_excerpt is None
    assert response.metadata.error.code is code
    assert outcome.status_code == status_for_error(code)
    assert response.metadata.error.message.strip(), "public message must still be informative"


def test_public_message_is_stable_per_code_whatever_the_adapter_says():
    def message_for(text: str) -> str:
        provider = RaisingProvider(
            ProviderError(ErrorKind.RATE_LIMITED, text, raw_excerpt=text), "leaky"
        )
        return generate(make_service(provider)).response.metadata.error.message

    assert message_for("first wording") == message_for("completely different wording")


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorKind.PROVIDER_ERROR, 502),
        (ErrorKind.RATE_LIMITED, 429),
        (ErrorKind.PROVIDER_TIMEOUT, 504),
        (ErrorKind.INTERNAL_ERROR, 500),
    ],
)
def test_classified_failure_keeps_code_and_status_while_hiding_text(code, status):
    provider = RaisingProvider(
        ProviderError(code, LEAKY_MESSAGE, raw_excerpt=LEAKY_EXCERPT), "leaky"
    )

    outcome = generate(make_service(provider))

    assert_canonical_failure(outcome, code=code, status=status)
    assert SECRET not in outcome.response.model_dump_json()


def test_adapter_error_text_is_not_written_to_the_server_log_either(caplog):
    provider = RaisingProvider(
        ProviderError(ErrorKind.PROVIDER_ERROR, LEAKY_MESSAGE, raw_excerpt=LEAKY_EXCERPT), "leaky"
    )

    with caplog.at_level(logging.DEBUG):
        generate(make_service(provider))

    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert "leaky" in logged and "provider_error" in logged, "the failure itself is still logged"
    assert SECRET not in logged and "Bearer" not in logged


def test_internally_generated_validation_excerpts_are_still_returned():
    bad = {"room_id": "x", "depth": 3, "room_type": "lava_temple", "size": "small"}

    error = generate(make_service(MalformedProvider(bad))).response.metadata.error

    assert "lava_temple" in (error.raw_excerpt or "")
    assert "room_type" in error.message


def test_invalid_json_excerpt_is_still_returned():
    error = generate(make_service(MalformedProvider("not { json"))).response.metadata.error

    assert error.code is ErrorKind.INVALID_JSON
    assert error.raw_excerpt == "not { json"


def test_unexpected_provider_exception_becomes_provider_error_without_leaking_its_text(caplog):
    boom = RaisingProvider(RuntimeError("connect https://api.example/?key=sk-super-secret failed"))

    with caplog.at_level(logging.ERROR, logger="dungeon_director"):
        outcome = generate(make_service(boom))

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=502)
    assert "RuntimeError" in outcome.response.metadata.error.message
    assert "sk-super-secret" not in outcome.response.model_dump_json()
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert "RuntimeError" in logged and "broken" in logged, "type and provider are logged"
    assert "sk-super-secret" not in logged and "api.example" not in logged
    assert not any(r.exc_info for r in caplog.records), "no traceback: it can carry secrets"


def test_one_provider_failure_does_not_break_the_next_request():
    good, bad = FakeProvider("good"), RaisingProvider(RuntimeError("boom"), "bad")
    service = make_service(good, bad)

    assert generate(service, provider="bad").response.success is False
    assert generate(service, provider="good").response.success is True
    assert generate(service, provider="bad").response.success is False
    assert generate(service).response.success is True


# --- malformed output --------------------------------------------------------


def test_malformed_dict_is_a_schema_violation_with_an_excerpt():
    bad = {"room_id": "x", "depth": 3, "room_type": "lava_temple", "size": "small", "danger": 9}

    outcome = generate(make_service(MalformedProvider(bad)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)
    error = outcome.response.metadata.error
    assert "room_type" in error.message
    assert "lava_temple" in (error.raw_excerpt or "")


def test_leaked_tile_geometry_is_rejected_as_a_schema_violation():
    request = make_request()
    room = {**valid_room_dict(request), "tiles": [[0, 1], [1, 0]]}

    outcome = generate(make_service(MalformedProvider(room)), request)

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)


def test_non_json_text_is_invalid_json():
    outcome = generate(make_service(MalformedProvider("Sure! Here is your room: {oops")))

    assert_canonical_failure(outcome, code=ErrorKind.INVALID_JSON, status=502)
    assert "Sure!" in outcome.response.metadata.error.raw_excerpt


@pytest.mark.parametrize("empty", ["", "   \n", b"", {}, None])
def test_empty_output_is_reported_as_empty_response(empty):
    outcome = generate(make_service(MalformedProvider(empty)))

    assert_canonical_failure(outcome, code=ErrorKind.EMPTY_RESPONSE, status=502)


@pytest.mark.parametrize("wrong_type", [42, ["a room"], 3.5])
def test_payload_of_the_wrong_type_is_a_schema_violation(wrong_type):
    outcome = generate(make_service(MalformedProvider(wrong_type)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)


def test_room_for_a_different_depth_is_rejected():
    request = make_request()
    room = {**valid_room_dict(request), "depth": request.state.depth + 1}

    outcome = generate(make_service(MalformedProvider(room)), request)

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)
    assert "depth" in outcome.response.metadata.error.message


def test_oversized_provider_metadata_cannot_break_the_envelope():
    class Chatty(FakeProvider):
        async def decide(self, request):
            return ProviderResult(
                payload=valid_room_dict(request), provider_metadata={"blob": "x" * 20_000}
            )

    outcome = generate(make_service(Chatty("chatty")))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)


@pytest.mark.parametrize("as_type", [dict, str, bytes, RoomPlan])
def test_valid_output_is_accepted_in_any_payload_form(as_type):
    request = make_request()
    room = valid_room_dict(request)
    payload = {
        dict: room,
        str: json.dumps(room),
        bytes: json.dumps(room).encode(),
        RoomPlan: RoomPlan.model_validate(room),
    }[as_type]

    outcome = generate(make_service(MalformedProvider(payload)), request)

    assert outcome.response.success is True
    assert outcome.response.room == RoomPlan.model_validate(room)


# --- isolation ---------------------------------------------------------------


def test_a_slow_request_does_not_block_a_fast_one():
    async def scenario():
        slow, fast = SlowProvider(), FakeProvider("fast")
        service = make_service(slow, fast, default="fast", timeout=0.3)
        slow_call = asyncio.create_task(service.generate(make_request(), provider="slow"))
        await asyncio.wait_for(slow.started.wait(), timeout=2)

        fast_outcome = await service.generate(make_request())
        slow_pending = not slow_call.done()
        slow_outcome = await slow_call
        return fast_outcome, slow_pending, slow_outcome

    fast_outcome, slow_pending, slow_outcome = asyncio.run(scenario())

    assert fast_outcome.response.success is True
    assert slow_pending is True
    assert slow_outcome.response.metadata.error.code is ErrorKind.PROVIDER_TIMEOUT


# --- availability checks that misbehave ---------------------------------------


def test_provider_whose_availability_check_raises_is_a_canonical_503(caplog):
    broken = BrokenAvailabilityProvider()

    with caplog.at_level(logging.ERROR):
        outcome = generate(make_service(FakeProvider("ok"), broken), provider="broken")

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=503)
    assert outcome.response.metadata.provider_metadata == {
        "selection_error": "provider_unavailable"
    }
    assert "availability probe crashed" not in outcome.response.model_dump_json()
    assert broken.calls == 0
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert "RuntimeError" in logged and "broken" in logged
    assert "availability probe crashed" not in logged
    assert not any(r.exc_info for r in caplog.records)


def test_default_provider_with_a_crashing_availability_check_fails_startup():
    with pytest.raises(DirectorConfigError, match="default provider"):
        make_service(BrokenAvailabilityProvider())


@pytest.mark.parametrize("bad_code", ["rate_limited_ish", None, 42])
def test_provider_error_with_a_nonstandard_code_still_yields_a_canonical_failure(bad_code):
    provider = RaisingProvider(ProviderError(bad_code, LEAKY_MESSAGE, raw_excerpt=LEAKY_EXCERPT))

    outcome = generate(make_service(provider))

    assert_canonical_failure(outcome, code=ErrorKind.PROVIDER_ERROR, status=502)
    assert SECRET not in outcome.response.model_dump_json()


# --- provider results are validated, never trusted ------------------------------


def _invalid_rooms(request):
    valid = valid_room_dict(request)
    constructed = RoomPlan.model_construct(**{**valid, "danger": 99, "room_type": "lava_temple"})
    mutated = RoomPlan.model_validate(valid)
    mutated.danger = 99  # plain assignment is not validated by the model
    nested = RoomPlan.model_validate(valid)
    nested.exits[0].direction = "sideways"
    duplicate_exits = RoomPlan.model_validate(
        {**valid, "exits": [{"direction": "south"}, {"direction": "east"}]}
    )
    duplicate_exits.exits[1].direction = duplicate_exits.exits[0].direction
    secret_without_probability = RoomPlan.model_validate(valid)
    secret_without_probability.has_secret = True
    return {
        "model_construct": constructed,
        "mutated_field": mutated,
        "mutated_nested_exit": nested,
        "mutated_duplicate_exit_directions": duplicate_exits,
        "mutated_secret_invariant": secret_without_probability,
    }


@pytest.mark.parametrize(
    "kind",
    [
        "model_construct",
        "mutated_field",
        "mutated_nested_exit",
        "mutated_duplicate_exit_directions",
        "mutated_secret_invariant",
    ],
)
def test_returned_room_plan_instances_are_revalidated_before_success(kind):
    request = make_request()
    bad_plan = _invalid_rooms(request)[kind]

    outcome = generate(make_service(MalformedProvider(bad_plan)), request)

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)
    assert outcome.response.room is None


def test_room_plan_instance_missing_required_fields_is_rejected_not_a_500():
    hollow = RoomPlan.model_construct(room_id="only-an-id")

    outcome = generate(make_service(MalformedProvider(hollow)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)


def test_valid_room_plan_instance_still_succeeds_after_revalidation():
    request = make_request()
    plan = RoomPlan.model_validate(valid_room_dict(request))

    outcome = generate(make_service(MalformedProvider(plan)), request)

    assert outcome.response.success is True
    assert outcome.response.room == plan


@pytest.mark.parametrize(
    "not_a_result",
    [{"payload": {"room_id": "x"}}, None, "text", 7, object(), RoomPlan.model_construct()],
)
def test_result_that_is_not_a_provider_result_is_a_canonical_502(not_a_result):
    outcome = generate(make_service(RawResultProvider(not_a_result)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)
    assert "invalid result" in outcome.response.metadata.error.message


def test_provider_result_with_unusable_metadata_is_a_canonical_failure_not_a_500():
    bad = ProviderResult(payload=valid_room_dict(make_request()), provider_metadata="not-a-dict")

    outcome = generate(make_service(RawResultProvider(bad)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)


class _Unrepresentable:
    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")


def _circular() -> dict:
    loop: dict = {"room_id": "x"}
    loop["self"] = loop
    return loop


@pytest.mark.parametrize(
    "payload",
    [
        {("tuple", "key"): 1},
        {1: object()},
        {b"bytes-key": 2},
        {"room_id": {1, 2, 3}},
        {"room_id": "x", "size": _Unrepresentable()},
        _circular(),
    ],
    ids=["tuple_key", "int_key_object_value", "bytes_key", "set_value", "bad_repr", "circular"],
)
def test_non_json_mapping_output_is_a_502_schema_violation_not_a_500(payload):
    outcome = generate(make_service(MalformedProvider(payload)))

    assert_canonical_failure(outcome, code=ErrorKind.SCHEMA_VIOLATION, status=502)
    assert outcome.response.metadata.error.raw_excerpt is not None


# --- telemetry survives a rejected provider result ----------------------------


def _usage() -> UsageStats:
    return UsageStats(input_tokens=812, output_tokens=231)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ('{"room_id": "x", "depth"', ErrorKind.INVALID_JSON),
        ({"room_id": "x"}, ErrorKind.SCHEMA_VIOLATION),
        ("", ErrorKind.EMPTY_RESPONSE),
        ({**valid_room_dict(make_request()), "depth": 99}, ErrorKind.SCHEMA_VIOLATION),
    ],
)
def test_rejected_provider_output_keeps_usage_and_provider_metadata(payload, code):
    result = ProviderResult(
        payload=payload, usage=_usage(), provider_metadata={"finish_reason": "length"}
    )

    outcome = generate(make_service(RawResultProvider(result)))

    assert_canonical_failure(outcome, code=code, status=502)
    metadata = outcome.response.metadata
    assert metadata.usage == _usage()
    assert metadata.provider_metadata == {"finish_reason": "length"}


def test_rejected_output_with_junk_telemetry_still_yields_a_canonical_failure():
    result = ProviderResult(payload="{bad", usage="lots", provider_metadata=["nope"])  # type: ignore[arg-type]

    outcome = generate(make_service(RawResultProvider(result)))

    assert_canonical_failure(outcome, code=ErrorKind.INVALID_JSON, status=502)
    assert outcome.response.metadata.usage is None
    assert outcome.response.metadata.provider_metadata == {}


def test_provider_reported_errors_still_carry_no_adapter_supplied_telemetry():
    outcome = generate(
        make_service(ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "secret text"))
    )

    assert_canonical_failure(outcome, code=ErrorKind.RATE_LIMITED, status=429)
    assert outcome.response.metadata.usage is None
    assert outcome.response.metadata.provider_metadata == {}


# --- untrusted usage objects are canonicalized, not merely type-checked --------


def _mutated_usage() -> UsageStats:
    usage = UsageStats(input_tokens=10, output_tokens=5)
    usage.output_tokens = -5  # no validate_assignment: mutation bypasses the bounds
    return usage


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            UsageStats.model_construct(input_tokens=-1, output_tokens=5),
            UsageStats(input_tokens=None, output_tokens=5),
        ),
        (_mutated_usage(), UsageStats(input_tokens=10, output_tokens=None)),
        (
            UsageStats.model_construct(input_tokens=99_999_999, output_tokens=2),
            UsageStats(input_tokens=None, output_tokens=2),
        ),
        (
            UsageStats.model_construct(input_tokens="12", output_tokens=True),  # type: ignore[arg-type]
            UsageStats(),
        ),
        (
            UsageStats.model_construct(input_tokens=1, estimated_cost_usd=float("inf")),
            UsageStats(input_tokens=1),
        ),
        (
            UsageStats.model_construct(input_tokens=1, estimated_cost_usd=float("nan")),
            UsageStats(input_tokens=1),
        ),
        (
            UsageStats.model_construct(estimated_cost_usd=-0.5),
            UsageStats(),
        ),
        (UsageStats(input_tokens=3, output_tokens=4), UsageStats(input_tokens=3, output_tokens=4)),
    ],
    ids=[
        "negative-input",
        "mutated-negative-output",
        "over-budget-input",
        "wrong-types",
        "infinite-cost",
        "nan-cost",
        "negative-cost",
        "valid-unchanged",
    ],
)
@pytest.mark.parametrize("rejected", [True, False], ids=["rejected-output", "successful-output"])
def test_usage_is_canonicalized_field_by_field_before_it_reaches_the_response(
    usage, expected, rejected
):
    payload = "{bad" if rejected else valid_room_dict(make_request())
    result = ProviderResult(payload=payload, usage=usage)

    outcome = generate(make_service(RawResultProvider(result)))

    assert outcome.response.success is (not rejected)
    assert outcome.response.metadata.usage == expected
    # The envelope must stay serializable and re-validatable as a whole.
    GenerationResponse.model_validate_json(outcome.response.model_dump_json())


def test_canonicalization_never_mutates_the_providers_own_usage_object():
    usage = UsageStats.model_construct(input_tokens=-1, output_tokens=5)

    generate(make_service(RawResultProvider(ProviderResult(payload="{bad", usage=usage))))

    assert usage.input_tokens == -1
