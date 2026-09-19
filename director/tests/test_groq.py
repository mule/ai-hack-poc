"""Groq GPT-OSS adapter: offline unit and contract tests (issue #9).

Everything here runs without network or credentials: the transport is a fake
that records requests and replays canned chat-completion bodies shaped like
Groq's documented OpenAI-compatible responses (see director/docs/groq.md).
The conftest offline guard additionally fails any test that touches a
non-loopback socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

import httpx
import pytest
from fakes import make_request, request_for_exit, request_payload

from dungeon_director.contracts import (
    EnvironmentalTag,
    ErrorKind,
    ExitDirection,
    ExitKind,
    GenerationResponse,
    RoomPlan,
    RoomSize,
    RoomType,
)
from dungeon_director.errors import DirectorConfigError, ProviderError
from dungeon_director.groq import (
    DEFAULT_GROQ_MODEL,
    GROQ_PROVIDER_ID,
    ROOM_PLAN_SCHEMA,
    GroqConfig,
    GroqProvider,
    GroqTransportRequest,
    GroqTransportResponse,
    HttpxGroqTransport,
    build_request_body,
)
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

FAKE_KEY = "gsk_test_secret_do_not_print"
BASE_URL = "https://api.groq.example/openai/v1"


class FakeTransport:
    """Records every request; replies with queued responses or raises."""

    def __init__(self, responses: list[Any] | None = None, error: BaseException | None = None):
        self.requests: list[GroqTransportRequest] = []
        self._responses = list(responses or [])
        self._error = error

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def http_response(
    body: Any, status: int = 200, headers: dict[str, str] | None = None
) -> GroqTransportResponse:
    if isinstance(body, bytes | bytearray):
        content = bytes(body)
    else:
        content = json.dumps(body).encode("utf-8")
    return GroqTransportResponse(status_code=status, headers=headers or {}, body=content)


def room_dict(**overrides: Any) -> dict[str, Any]:
    """A complete strict-schema room: every RoomPlan key present (depth 3, north frontier)."""
    room: dict[str, Any] = {
        "room_id": "groq-0123456789",
        "depth": 3,
        "room_type": "chamber",
        "size": "medium",
        "danger": 2,
        "exits": [
            {"direction": "south", "kind": "door", "locked": False},
            {"direction": "east", "kind": "door", "locked": False},
        ],
        "enemy_density": 0.2,
        "loot_density": 0.4,
        "secret_probability": 0.0,
        "has_secret": False,
        "environmental_tags": ["dark"],
        "description": "A dim worked-stone chamber.",
    }
    room.update(overrides)
    return room


def completion(
    content: Any = None,
    *,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
    message: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A chat completion shaped like Groq's documented OpenAI-compatible response."""
    if content is None:
        content = json.dumps(room_dict())
    body: dict[str, Any] = {
        "id": "chatcmpl-test-1",
        "object": "chat.completion",
        "created": 1_780_000_000,
        "model": "openai/gpt-oss-20b",
        "choices": [
            {
                "index": 0,
                "message": message or {"role": "assistant", "content": content},
                "logprobs": None,
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage
        if usage is not None
        else {
            "queue_time": 0.0421,
            "prompt_tokens": 812,
            "prompt_time": 0.0041,
            "completion_tokens": 231,
            "completion_time": 0.1123,
            "total_tokens": 1043,
            "total_time": 0.1164,
            "completion_tokens_details": {"reasoning_tokens": 37},
        },
        "system_fingerprint": "fp_test",
        "x_groq": {"id": "req_01test"},
        "service_tier": "on_demand",
    }
    body.update(extra)
    return body


def config(**overrides: Any) -> GroqConfig:
    values: dict[str, Any] = {"api_base_url": BASE_URL, "api_key": FAKE_KEY}
    values.update(overrides)
    return GroqConfig(**values)


def provider_with(transport: Any, **config_overrides: Any) -> GroqProvider:
    return GroqProvider(config(**config_overrides), transport)


def generate(transport: FakeTransport, request: Any = None, **config_overrides: Any):
    provider = provider_with(transport, **config_overrides)
    return asyncio.run(provider.generate(request or make_request(), model=provider.default_model))


def provider_error(transport: FakeTransport, **config_overrides: Any) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        generate(transport, **config_overrides)
    return info.value


def service_outcome(transport: FakeTransport, *, timeout: float = 5.0, request: Any = None):
    registry = ProviderRegistry()
    registry.register(provider_with(transport))
    settings = DirectorSettings(default_provider=GROQ_PROVIDER_ID, timeout_seconds=timeout)
    service = DirectorService(registry, settings)
    return asyncio.run(service.generate(request or make_request()))


# --- identifiers, configuration, availability ---------------------------------


def test_provider_has_stable_id_and_configurable_model():
    provider = provider_with(FakeTransport(), model="openai/gpt-oss-120b")

    assert provider.provider_id == GROQ_PROVIDER_ID == "groq"
    assert provider.models == ("openai/gpt-oss-120b",)
    assert provider.default_model == "openai/gpt-oss-120b"
    assert DEFAULT_GROQ_MODEL == "openai/gpt-oss-20b"


def test_config_defaults_target_gpt_oss_20b_with_minimal_reasoning():
    empty = GroqConfig.from_env({})

    assert empty.model == "openai/gpt-oss-20b"
    assert empty.api_base_url == "https://api.groq.com/openai/v1"
    assert empty.reasoning_effort == "low"
    assert empty.max_completion_tokens == 2048
    assert empty.has_credentials is False


def test_config_reads_every_environment_variable_and_trims_values():
    configured = GroqConfig.from_env(
        {
            "GROQ_API_KEY": "  key-1  ",
            "GROQ_MODEL": " openai/gpt-oss-120b ",
            "GROQ_API_BASE_URL": "https://proxy.example/openai/v1/",
            "GROQ_REASONING_EFFORT": "medium",
            "GROQ_MAX_COMPLETION_TOKENS": "4096",
        }
    )

    assert configured.has_credentials is True
    assert configured.model == "openai/gpt-oss-120b"
    assert configured.chat_completions_url == "https://proxy.example/openai/v1/chat/completions"
    assert configured.reasoning_effort == "medium"
    assert configured.max_completion_tokens == 4096
    assert configured.authorization_header() == {"Authorization": "Bearer key-1"}


def test_blank_environment_values_mean_defaults():
    configured = GroqConfig.from_env(
        {
            "GROQ_API_KEY": "  ",
            "GROQ_MODEL": "",
            "GROQ_API_BASE_URL": " ",
            "GROQ_REASONING_EFFORT": "",
            "GROQ_MAX_COMPLETION_TOKENS": "  ",
        }
    )

    assert configured == GroqConfig()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GROQ_MODEL", "bad model id"),
        ("GROQ_MODEL", "openai/gpt-oss-20b\nX-Injected: 1"),
        ("GROQ_API_BASE_URL", "ftp://api.groq.example"),
        ("GROQ_API_BASE_URL", "http://api.groq.example/openai/v1"),
        ("GROQ_API_BASE_URL", "api.groq.example"),
        ("GROQ_REASONING_EFFORT", "turbo"),
        ("GROQ_REASONING_EFFORT", "none"),  # GPT-OSS accepts only low/medium/high
        ("GROQ_REASONING_EFFORT", "minimal"),
        ("GROQ_MAX_COMPLETION_TOKENS", "abc"),
        ("GROQ_MAX_COMPLETION_TOKENS", "0"),
        ("GROQ_MAX_COMPLETION_TOKENS", "-5"),
        ("GROQ_MAX_COMPLETION_TOKENS", "1000000"),
        ("GROQ_MAX_COMPLETION_TOKENS", "12.5"),
    ],
)
def test_invalid_configuration_names_the_variable_and_never_echoes_secrets(name, value):
    with pytest.raises(DirectorConfigError) as info:
        GroqConfig.from_env({"GROQ_API_KEY": FAKE_KEY, name: value})

    assert name in str(info.value)
    assert FAKE_KEY not in str(info.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", "gsk_key\n"),
        ("api_key", "gsk_key\r\nX-Injected: 1"),
        ("api_key", "gsk key"),
        ("api_key", "gsk_key\x00"),
        ("model", "openai/gpt-oss-20b\n"),
        ("model", "openai/gpt-oss-20b\r\nX-Injected: 1"),
        ("api_base_url", "https://api.groq.example/openai/v1\n"),
        ("api_base_url", "https://api.groq.example/open ai/v1"),
    ],
)
def test_direct_construction_rejects_trailing_newlines_and_control_characters(field, value):
    # `$` matches before a final "\n", so a `re.match` on an anchored pattern would
    # let these through and leave header/URL injection to httpx. `from_env`
    # strips whitespace, so only direct construction can carry them.
    with pytest.raises(DirectorConfigError) as info:
        GroqConfig(**{"api_key": "k", **{field: value}})

    assert value not in str(info.value)


def test_telemetry_labels_with_trailing_newlines_are_dropped():
    body = completion(x_groq={"id": "req_01test\n"}, service_tier="on_demand\n")
    body["model"] = "openai/gpt-oss-20b\n"
    body["choices"][0]["finish_reason"] = "stop\n"

    metadata = generate(FakeTransport([http_response(body)])).provider_metadata

    for key in ("groq_request_id", "service_tier", "groq_model", "finish_reason"):
        assert key not in metadata, key


def test_non_gpt_oss_models_may_use_the_other_documented_reasoning_efforts():
    # Qwen models document `none`; only the GPT-OSS family is restricted.
    qwen = GroqConfig(model="qwen/qwen3.8-27b", reasoning_effort="none", api_key="k")

    assert qwen.reasoning_effort == "none"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_http_base_url_is_allowed_for_local_integration_tests(host):
    config_ = GroqConfig(api_key="k", api_base_url=f"http://{host}:8080/openai/v1")

    assert config_.chat_completions_url.endswith("/openai/v1/chat/completions")


def test_availability_reports_a_missing_key_by_variable_name_only():
    provider = GroqProvider.from_env({})

    availability = provider.availability

    assert availability.available is False
    assert "GROQ_API_KEY" in availability.reason
    assert provider_with(FakeTransport()).availability.available is True


def test_credentials_never_appear_in_reprs():
    provider = provider_with(FakeTransport())

    text = f"{provider!r} {provider._config!r}"

    assert FAKE_KEY not in text
    assert "configured=True" in repr(provider)


def test_generate_without_a_key_is_a_classified_error_and_sends_nothing():
    transport = FakeTransport()
    provider = GroqProvider(GroqConfig(), transport)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model=DEFAULT_GROQ_MODEL))

    assert info.value.code is ErrorKind.PROVIDER_ERROR
    assert transport.requests == []


def test_generate_rejects_a_model_the_provider_was_not_configured_with():
    transport = FakeTransport()
    provider = provider_with(transport)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model="some/other-model"))

    assert info.value.code is ErrorKind.PROVIDER_ERROR
    assert transport.requests == []


# --- the strict structured-output schema --------------------------------------

_UNSUPPORTED_STRICT_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minItems",
    "maxItems",
    "default",
    "$ref",
    "$defs",
    "oneOf",
    "allOf",
}


def _schema_objects(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            yield node
        for value in node.values():
            yield from _schema_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_objects(value)


def _schema_keywords(node: Any):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            if key != "properties":
                yield from _schema_keywords(value)
            else:
                for sub in value.values():
                    yield from _schema_keywords(sub)
    elif isinstance(node, list):
        for value in node:
            yield from _schema_keywords(value)


def test_schema_is_strict_mode_compatible():
    objects = list(_schema_objects(ROOM_PLAN_SCHEMA))

    assert len(objects) >= 2, "room plus the nested exit object"
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"]), "strict mode: every field required"
    assert not (set(_schema_keywords(ROOM_PLAN_SCHEMA)) & _UNSUPPORTED_STRICT_KEYWORDS)


def test_schema_covers_exactly_the_room_plan_fields_and_enums():
    properties = ROOM_PLAN_SCHEMA["properties"]
    exit_props = properties["exits"]["items"]["properties"]

    assert set(properties) == set(RoomPlan.model_fields), "schema drifted from the contract"
    assert set(properties["room_type"]["enum"]) == {t.value for t in RoomType}
    assert set(properties["size"]["enum"]) == {s.value for s in RoomSize}
    assert properties["danger"]["enum"] == [1, 2, 3, 4, 5]
    assert set(properties["environmental_tags"]["items"]["enum"]) == {
        t.value for t in EnvironmentalTag
    }
    assert set(exit_props["direction"]["enum"]) == {d.value for d in ExitDirection}
    assert set(exit_props["kind"]["enum"]) == {k.value for k in ExitKind}
    assert set(properties["exits"]["items"]["required"]) == {"direction", "kind", "locked"}


def test_optional_fields_are_nullable_unions_not_missing_keys():
    properties = ROOM_PLAN_SCHEMA["properties"]

    assert properties["has_secret"]["type"] == ["boolean", "null"]
    assert properties["description"]["type"] == ["string", "null"]


def test_a_schema_complete_room_validates_as_a_room_plan():
    room = RoomPlan.model_validate(room_dict())

    assert room.depth == 3
    assert set(room_dict()) == set(ROOM_PLAN_SCHEMA["properties"])


# --- request shape ------------------------------------------------------------


def test_request_matches_documented_groq_chat_completions_contract():
    transport = FakeTransport([http_response(completion())])

    generate(transport)

    (sent,) = transport.requests
    assert sent.method == "POST"
    assert sent.url == f"{BASE_URL}/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert sent.headers["Content-Type"] == "application/json"
    body = sent.json_body
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["max_completion_tokens"] == 2048
    assert "max_tokens" not in body, "max_tokens is deprecated in favour of max_completion_tokens"
    assert isinstance(body["seed"], int) and not isinstance(body["seed"], bool)


def test_request_demands_strict_json_schema_output():
    body = build_request_body(make_request(), config(), "openai/gpt-oss-20b")

    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "room_plan", "strict": True, "schema": ROOM_PLAN_SCHEMA},
    }


def test_gpt_oss_reasoning_is_minimized_with_only_documented_parameters():
    body = build_request_body(make_request(), config(), "openai/gpt-oss-20b")

    assert body["reasoning_effort"] == "low"
    assert body["include_reasoning"] is False
    # GPT-OSS rejects `none`/`minimal`, and reasoning_format is mutually
    # exclusive with include_reasoning (and unsupported on GPT-OSS).
    assert "reasoning_format" not in body
    assert body["reasoning_effort"] not in {"none", "minimal"}


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_reasoning_effort_is_configurable_within_the_gpt_oss_range(effort):
    body = build_request_body(make_request(), config(reasoning_effort=effort), DEFAULT_GROQ_MODEL)

    assert body["reasoning_effort"] == effort


def test_include_reasoning_is_only_sent_for_the_gpt_oss_family():
    qwen = config(model="qwen/qwen3.8-27b", reasoning_effort="none")

    body = build_request_body(make_request(), qwen, "qwen/qwen3.8-27b")

    assert body["reasoning_effort"] == "none"
    assert "include_reasoning" not in body


def test_max_completion_tokens_is_configurable():
    body = build_request_body(make_request(), config(max_completion_tokens=777), DEFAULT_GROQ_MODEL)

    assert body["max_completion_tokens"] == 777


def test_requests_are_deterministic_for_benchmarking():
    first = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)
    second = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)

    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_seed_is_derived_from_the_request_identity():
    base = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)
    other = build_request_body(make_request(request_id="req-other"), config(), DEFAULT_GROQ_MODEL)

    assert base["seed"] != other["seed"]
    assert 0 <= base["seed"] < 2**31


def test_messages_are_a_system_rule_prompt_and_a_compact_json_user_state():
    body = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)

    system, user = body["messages"]
    assert [system["role"], user["role"]] == ["system", "user"]
    state = json.loads(user["content"])
    assert state["depth"] == 3
    assert state["frontier"] == {"room_id": "r-003", "direction": "north"}
    assert state["back_direction"] == "south"
    assert state["room_id"].startswith("groq-")
    assert state["player"]["hp"] == 22
    assert state["options"]["forbidden_room_types"] == ["vault"]
    assert state["options"]["max_danger"] == 3
    assert "cautious" in state["hint"]
    assert user["content"] == json.dumps(state, separators=(",", ":"), sort_keys=True)


def test_vertical_frontier_asks_for_a_stairs_back_link():
    request = request_for_exit("down")

    state = json.loads(
        build_request_body(request, config(), DEFAULT_GROQ_MODEL)["messages"][1]["content"]
    )

    assert state["back_direction"] == "up"
    assert state["back_kind"] == "stairs"


def test_prompt_stays_compact_even_for_a_maximal_request():
    payload = request_payload()
    payload["state"]["recent_rooms"] = [
        {"room_id": f"r-{i:03d}", "room_type": "room", "danger": 3} for i in range(32)
    ]
    payload["state"]["recent_events"] = [{"event": "e" * 120, "turn": i} for i in range(32)]
    payload["state"]["inventory"] = [
        {"item_id": f"item_{i:03d}", "quantity": 9999, "category": "tool"} for i in range(128)
    ]
    payload["prompt_hint"] = "h" * 500
    request = type(make_request()).model_validate(payload)

    body = build_request_body(request, config(), DEFAULT_GROQ_MODEL)
    prompt_chars = sum(len(message["content"]) for message in body["messages"])

    assert prompt_chars < 6000
    typical = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)
    assert sum(len(m["content"]) for m in typical["messages"]) < 3500


def test_system_prompt_states_the_pacing_and_contract_rules_the_model_must_follow():
    system = build_request_body(make_request(), config(), DEFAULT_GROQ_MODEL)["messages"][0][
        "content"
    ]

    for fragment in ("room_id", "depth", "stairs_down", "allow_secrets", "max_danger"):
        assert fragment in system


def test_no_credentials_appear_in_the_request_body():
    transport = FakeTransport([http_response(completion())])

    generate(transport)

    assert FAKE_KEY not in json.dumps(transport.requests[0].json_body)


# --- successful decisions ------------------------------------------------------


def test_success_returns_the_model_json_untouched_for_the_service_to_validate():
    content = json.dumps(room_dict())
    transport = FakeTransport([http_response(completion(content))])

    result = generate(transport)

    assert result.payload == content, "the adapter must not repair or re-serialize the answer"


def test_success_exposes_token_usage():
    result = generate(FakeTransport([http_response(completion())]))

    assert result.usage is not None
    assert result.usage.input_tokens == 812
    assert result.usage.output_tokens == 231
    assert result.usage.estimated_cost_usd is None, "no invented pricing"


def test_success_exposes_groq_timing_and_request_metadata():
    result = generate(FakeTransport([http_response(completion())]))

    metadata = result.provider_metadata
    assert metadata["queue_time_s"] == pytest.approx(0.0421)
    assert metadata["prompt_time_s"] == pytest.approx(0.0041)
    assert metadata["completion_time_s"] == pytest.approx(0.1123)
    assert metadata["total_time_s"] == pytest.approx(0.1164)
    assert metadata["prompt_tokens"] == 812
    assert metadata["completion_tokens"] == 231
    assert metadata["total_tokens"] == 1043
    assert metadata["reasoning_tokens"] == 37
    assert metadata["finish_reason"] == "stop"
    assert metadata["groq_request_id"] == "req_01test"
    assert metadata["groq_model"] == "openai/gpt-oss-20b"
    assert metadata["service_tier"] == "on_demand"
    assert metadata["reasoning_effort"] == "low"
    assert metadata["max_completion_tokens"] == 2048
    assert metadata["strict_schema"] is True
    assert metadata["upstream_http_status"] == 200
    assert metadata["adapter_elapsed_ms"] >= 0
    assert isinstance(metadata["seed"], int)


def test_metadata_is_json_safe_and_inside_the_contract_budget():
    metadata = generate(FakeTransport([http_response(completion())])).provider_metadata

    assert len(metadata) <= 32
    assert len(json.dumps(metadata, separators=(",", ":")).encode("utf-8")) <= 8192
    assert FAKE_KEY not in json.dumps(metadata)


def test_missing_optional_usage_fields_are_simply_absent_not_invented():
    body = completion(usage={"prompt_tokens": 10, "completion_tokens": 5})
    del body["x_groq"], body["service_tier"], body["system_fingerprint"]

    result = generate(FakeTransport([http_response(body)]))

    assert result.usage is not None and result.usage.input_tokens == 10
    for key in ("queue_time_s", "total_time_s", "reasoning_tokens", "groq_request_id"):
        assert key not in result.provider_metadata


def test_absent_usage_object_yields_no_usage_stats():
    body = completion()
    del body["usage"]

    result = generate(FakeTransport([http_response(body)]))

    assert result.usage is None


def test_hostile_usage_and_metadata_values_are_dropped_not_coerced():
    body = completion(
        usage={
            "prompt_tokens": True,
            "completion_tokens": "231",
            "queue_time": float("inf"),
            "prompt_time": -1.0,
            "completion_time": "0.1",
            "total_time": None,
            "completion_tokens_details": {"reasoning_tokens": -4},
        },
        x_groq={"id": "req id with spaces\nand newline" + "x" * 200},
        service_tier="t" * 500,
    )
    raw = json.dumps(body).replace("Infinity", "1e999")

    result = generate(FakeTransport([http_response(raw.encode())]))

    assert result.usage is not None
    assert result.usage.input_tokens is None and result.usage.output_tokens is None
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "queue_time_s",
        "prompt_time_s",
        "completion_time_s",
        "total_time_s",
        "reasoning_tokens",
        "groq_request_id",
    ):
        assert key not in result.provider_metadata, key
    assert len(result.provider_metadata.get("service_tier", "")) <= 32


def test_reasoning_text_is_never_copied_into_the_result():
    message = {
        "role": "assistant",
        "content": json.dumps(room_dict()),
        "reasoning": "secret chain of thought " * 100,
    }

    result = generate(FakeTransport([http_response(completion(message=message))]))

    assert "chain of thought" not in json.dumps(result.provider_metadata)
    assert isinstance(result.payload, str) and "chain of thought" not in result.payload


def test_only_the_first_choice_is_used():
    body = completion()
    other = json.dumps(room_dict(room_type="shrine"))
    body["choices"].append(
        {"index": 1, "message": {"role": "assistant", "content": other}, "finish_reason": "stop"}
    )

    result = generate(FakeTransport([http_response(body)]))

    assert '"chamber"' in result.payload


def test_one_generate_is_exactly_one_http_call_even_on_failure():
    for reply in (
        http_response({"error": {"message": "slow down", "type": "rate_limit"}}, status=429),
        http_response({"error": {"message": "boom"}}, status=500),
        http_response(completion("{not json")),
    ):
        transport = FakeTransport([reply])
        try:
            generate(transport)
        except ProviderError:
            pass
        assert len(transport.requests) == 1, "no hidden retries"


def test_end_to_end_success_through_the_service_yields_a_canonical_response():
    outcome = service_outcome(FakeTransport([http_response(completion())]))

    response = outcome.response
    assert outcome.status_code == 200
    assert response.success is True
    assert isinstance(response.room, RoomPlan)
    assert response.room.room_type is RoomType.CHAMBER
    assert response.metadata.provider == "groq"
    assert response.metadata.model == "openai/gpt-oss-20b"
    assert response.metadata.usage is not None
    assert response.metadata.usage.input_tokens == 812
    assert response.metadata.provider_metadata["queue_time_s"] == pytest.approx(0.0421)
    assert response.metadata.latency_ms is not None
    GenerationResponse.model_validate(response.model_dump(mode="json"))


# --- invalid structured output is a recorded failure, never silently repaired --


def failed(outcome):
    assert outcome.response.success is False
    assert outcome.response.room is None
    return outcome.response.metadata


def test_malformed_json_is_recorded_as_invalid_json_with_excerpt_usage_and_metadata():
    truncated = '{"room_id": "groq-1", "depth": 3, "room_ty'
    outcome = service_outcome(
        FakeTransport([http_response(completion(truncated, finish_reason="length"))])
    )

    metadata = failed(outcome)
    assert outcome.status_code == 502
    assert metadata.error.code is ErrorKind.INVALID_JSON
    assert metadata.error.raw_excerpt == truncated
    assert metadata.usage is not None and metadata.usage.output_tokens == 231
    assert metadata.provider_metadata["finish_reason"] == "length"
    assert metadata.provider_metadata["groq_request_id"] == "req_01test"


@pytest.mark.parametrize(
    "bad_room",
    [
        room_dict(danger=9),
        room_dict(enemy_density=1.5),
        room_dict(room_type="dragon_lair"),
        room_dict(depth=4),  # another frontier's depth
        room_dict(has_secret=True, secret_probability=0.0),
        room_dict(exits=[{"direction": "south", "kind": "door", "locked": False}] * 2),
        {**room_dict(), "extra_field": 1},
        {k: v for k, v in room_dict().items() if k != "size"},
    ],
    ids=[
        "danger-out-of-range",
        "density-out-of-range",
        "unknown-room-type",
        "wrong-depth",
        "secret-invariant",
        "duplicate-exit-directions",
        "unknown-field",
        "missing-field",
    ],
)
def test_schema_violating_output_is_a_recorded_failure_with_usage_not_a_repair(bad_room):
    outcome = service_outcome(FakeTransport([http_response(completion(json.dumps(bad_room)))]))

    metadata = failed(outcome)
    assert metadata.error.code is ErrorKind.SCHEMA_VIOLATION
    assert metadata.usage is not None and metadata.usage.input_tokens == 812
    assert metadata.provider_metadata["groq_request_id"] == "req_01test"


def test_non_object_json_output_is_a_recorded_failure():
    metadata = failed(service_outcome(FakeTransport([http_response(completion("[1, 2, 3]"))])))

    assert metadata.error.code is ErrorKind.SCHEMA_VIOLATION
    assert metadata.usage is not None


@pytest.mark.parametrize("content", ["", None, "   "])
def test_empty_content_is_recorded_as_empty_response_with_usage(content):
    # Typical when reasoning consumed the whole max_completion_tokens budget.
    body = completion(message={"role": "assistant", "content": content}, finish_reason="length")

    outcome = service_outcome(FakeTransport([http_response(body)]))

    metadata = failed(outcome)
    assert metadata.error.code is ErrorKind.EMPTY_RESPONSE
    assert metadata.usage is not None and metadata.usage.output_tokens == 231
    assert metadata.provider_metadata["finish_reason"] == "length"


def test_a_refusal_is_a_safety_refusal():
    body = completion(message={"role": "assistant", "content": None, "refusal": "I can't do that"})

    error = provider_error(FakeTransport([http_response(body)]))

    assert error.code is ErrorKind.SAFETY_REFUSAL
    assert "can't do that" not in error.message


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": "nope"},
        {"choices": [None]},
        {"choices": [{"index": 0}]},
        {"choices": [{"message": "text"}]},
        {"choices": [{"message": {"role": "assistant", "content": {"not": "a string"}}}]},
        {"choices": [{"message": {"role": "assistant", "content": 42}}]},
    ],
)
def test_malformed_completion_shapes_are_schema_violations(body):
    error = provider_error(FakeTransport([http_response(body)]))

    assert error.code is ErrorKind.SCHEMA_VIOLATION


def test_a_200_body_carrying_an_error_object_is_a_provider_error():
    body = {"error": {"message": f"echoing {FAKE_KEY}", "type": "server_error"}}

    error = provider_error(FakeTransport([http_response(body)]))

    assert error.code is ErrorKind.PROVIDER_ERROR
    assert FAKE_KEY not in error.message


def test_non_json_and_non_object_bodies_are_classified():
    invalid = provider_error(FakeTransport([http_response(b"<html>gateway</html>")]))
    not_utf8 = provider_error(FakeTransport([http_response(b"\xff\xfe\x00")]))
    array = provider_error(FakeTransport([http_response([1, 2])]))

    assert invalid.code is ErrorKind.INVALID_JSON
    assert not_utf8.code is ErrorKind.INVALID_JSON
    assert array.code is ErrorKind.SCHEMA_VIOLATION


# --- HTTP error classification and sanitization ------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorKind.PROVIDER_ERROR),
        (401, ErrorKind.PROVIDER_ERROR),
        (403, ErrorKind.PROVIDER_ERROR),
        (404, ErrorKind.PROVIDER_ERROR),
        (413, ErrorKind.PROVIDER_ERROR),
        (422, ErrorKind.PROVIDER_ERROR),
        (424, ErrorKind.PROVIDER_ERROR),
        (429, ErrorKind.RATE_LIMITED),
        (498, ErrorKind.RATE_LIMITED),  # Groq flex-tier capacity exceeded
        (499, ErrorKind.PROVIDER_ERROR),
        (500, ErrorKind.PROVIDER_ERROR),
        (502, ErrorKind.PROVIDER_ERROR),
        (503, ErrorKind.PROVIDER_ERROR),
        (201, ErrorKind.PROVIDER_ERROR),
        (302, ErrorKind.PROVIDER_ERROR),
    ],
)
def test_http_statuses_map_to_stable_error_kinds(status, code):
    error = provider_error(FakeTransport([http_response({"error": {"message": "x"}}, status)]))

    assert error.code is code


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_error_text_never_echoes_upstream_bodies_urls_or_credentials(status):
    body = {"error": {"message": f"bad key {FAKE_KEY} at {BASE_URL}", "type": "invalid_request"}}

    error = provider_error(FakeTransport([http_response(body, status)]))

    haystack = f"{error} {error.message} {error.raw_excerpt}"
    assert FAKE_KEY not in haystack
    assert BASE_URL not in haystack
    assert "bad key" not in haystack


@pytest.mark.parametrize("status", [401, 429, 500])
def test_service_hides_adapter_error_text_and_maps_status(status, caplog):
    body = {"error": {"message": f"bad key {FAKE_KEY}"}}

    with caplog.at_level(logging.DEBUG):
        outcome = service_outcome(FakeTransport([http_response(body, status)]))

    wire = outcome.response.model_dump_json()
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_KEY not in wire and FAKE_KEY not in logged
    assert outcome.status_code == (429 if status == 429 else 502)
    assert outcome.response.metadata.error.code is (
        ErrorKind.RATE_LIMITED if status == 429 else ErrorKind.PROVIDER_ERROR
    )


# --- transport failures, timeouts and cancellation ---------------------------


def test_transport_exceptions_are_classified_without_their_text():
    error = provider_error(
        FakeTransport(error=RuntimeError(f"connect failed for {BASE_URL} with {FAKE_KEY}"))
    )

    assert error.code is ErrorKind.PROVIDER_ERROR
    assert FAKE_KEY not in error.message and BASE_URL not in error.message
    assert "RuntimeError" in error.message


def test_httpx_timeouts_are_provider_timeouts():
    error = provider_error(FakeTransport(error=httpx.ReadTimeout("read timed out")))

    assert error.code is ErrorKind.PROVIDER_TIMEOUT


def test_other_httpx_errors_are_provider_errors_naming_only_the_type():
    error = provider_error(FakeTransport(error=httpx.ConnectError(f"refused {FAKE_KEY}")))

    assert error.code is ErrorKind.PROVIDER_ERROR
    assert "ConnectError" in error.message and FAKE_KEY not in error.message


def test_cancellation_propagates_from_the_transport():
    transport = FakeTransport(error=asyncio.CancelledError())
    provider = provider_with(transport)

    async def run() -> None:
        await provider.generate(make_request(), model=provider.default_model)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run())


class HangingTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.sent = 0

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse:
        self.sent += 1
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


def test_director_deadline_cancels_the_in_flight_groq_call():
    transport = HangingTransport()
    registry = ProviderRegistry()
    registry.register(provider_with(transport))
    service = DirectorService(
        registry, DirectorSettings(default_provider=GROQ_PROVIDER_ID, timeout_seconds=0.05)
    )

    outcome = asyncio.run(service.generate(make_request()))

    assert outcome.status_code == 504
    assert outcome.response.metadata.error.code is ErrorKind.PROVIDER_TIMEOUT
    assert outcome.response.metadata.provider_metadata == {"timeout_origin": "director_deadline"}
    assert transport.cancelled is True
    assert transport.sent == 1, "a timeout is not retried"


def test_task_cancellation_reaches_the_groq_transport():
    transport = HangingTransport()
    provider = provider_with(transport)

    async def run() -> bool:
        task = asyncio.create_task(provider.generate(make_request(), model=provider.default_model))
        await transport.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return transport.cancelled

    assert asyncio.run(run()) is True


# --- the real httpx transport (mocked at the httpx layer) --------------------


class CountingTransport:
    """Stands in for HttpxGroqTransport: counts creations, sends and closes."""

    instances: list[CountingTransport] = []

    def __init__(self) -> None:
        self.sent = 0
        self.closed = False
        CountingTransport.instances.append(self)

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse:
        self.sent += 1
        return http_response(completion())

    async def aclose(self) -> None:
        self.closed = True


def test_default_transport_is_created_once_and_reused(monkeypatch):
    import dungeon_director.groq as groq

    CountingTransport.instances = []
    monkeypatch.setattr(groq, "HttpxGroqTransport", CountingTransport)
    provider = GroqProvider(config())

    asyncio.run(provider.generate(make_request(), model=provider.default_model))
    asyncio.run(provider.generate(make_request(), model=provider.default_model))

    assert len(CountingTransport.instances) == 1, "one shared client, not one per call"
    assert CountingTransport.instances[0].sent == 2


def test_aclose_closes_an_owned_transport_and_leaves_injected_transports_alone(monkeypatch):
    import dungeon_director.groq as groq

    CountingTransport.instances = []
    monkeypatch.setattr(groq, "HttpxGroqTransport", CountingTransport)
    provider = GroqProvider(config())
    asyncio.run(provider.generate(make_request(), model=provider.default_model))
    owned = CountingTransport.instances[0]

    asyncio.run(provider.aclose())

    assert owned.closed is True
    injected = CountingTransport()
    asyncio.run(GroqProvider(config(), injected).aclose())
    assert injected.closed is False, "injected transports belong to their owner"


def test_real_httpx_transport_maps_the_request_onto_the_wire():
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion())

    async def exercise() -> ProviderResult:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GroqProvider(config(), HttpxGroqTransport(client))
        try:
            return await provider.generate(make_request(), model=provider.default_model)
        finally:
            await client.aclose()

    result = asyncio.run(exercise())

    assert isinstance(result.payload, str)
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == f"{BASE_URL}/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(seen[0].content)["response_format"]["json_schema"]["strict"] is True


def test_real_httpx_transport_does_not_follow_redirects():
    hits: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/steal"})

    async def exercise() -> ProviderError:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        provider = GroqProvider(config(), HttpxGroqTransport(client))
        try:
            with pytest.raises(ProviderError) as info:
                await provider.generate(make_request(), model=provider.default_model)
            return info.value
        finally:
            await client.aclose()

    error = asyncio.run(exercise())

    assert error.code is ErrorKind.PROVIDER_ERROR
    assert hits == [f"{BASE_URL}/chat/completions"]


def test_owned_httpx_client_is_configured_without_redirects_or_own_timeout():
    async def exercise() -> tuple[bool, Any]:
        transport = HttpxGroqTransport()
        try:
            return transport._client.follow_redirects, transport._client.timeout
        finally:
            await transport.aclose()

    follow, timeout = asyncio.run(exercise())

    assert follow is False
    assert timeout == httpx.Timeout(None), "the director's deadline is the only timeout"


def test_real_httpx_transport_caps_streamed_response_bodies():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1_048_577)

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GroqProvider(config(), HttpxGroqTransport(client))
        try:
            with pytest.raises(ProviderError) as info:
                await provider.generate(make_request(), model=provider.default_model)
            assert info.value.code is ErrorKind.PROVIDER_ERROR
        finally:
            await client.aclose()

    asyncio.run(exercise())


def test_real_httpx_transport_propagates_midflight_cancellation():
    async def exercise() -> bool:
        started = asyncio.Event()
        cancelled = False

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal cancelled
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled = True
                raise
            return httpx.Response(200, json=completion())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = GroqProvider(config(), HttpxGroqTransport(client))
        task = asyncio.create_task(provider.generate(make_request(), model=provider.default_model))
        await started.wait()
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
            return cancelled
        finally:
            await client.aclose()

    assert asyncio.run(exercise()) is True


def test_app_lifespan_closes_the_owned_groq_transport(monkeypatch):
    from fastapi.testclient import TestClient

    import dungeon_director.groq as groq
    from dungeon_director.app import create_app

    CountingTransport.instances = []
    monkeypatch.setattr(groq, "HttpxGroqTransport", CountingTransport)
    registry = ProviderRegistry()
    registry.register(GroqProvider(config()))
    settings = DirectorSettings(default_provider=GROQ_PROVIDER_ID, timeout_seconds=1.0)

    with TestClient(create_app(settings=settings, registry=registry)) as client:
        response = client.post("/v1/generate", json=request_payload())
        assert response.status_code == 200
        assert response.json()["metadata"]["provider"] == "groq"
        owned = CountingTransport.instances[0]
        assert owned.closed is False

    assert owned.closed is True


# --- the offline guard itself --------------------------------------------------


def test_offline_guard_refuses_non_loopback_dns_and_connects(offline_guard):
    with pytest.raises(OSError, match="network access is disabled"):
        socket.getaddrinfo("api.groq.com", 443)
    with pytest.raises(OSError, match="network access is disabled"):
        socket.socket().connect(("203.0.113.7", 443))

    assert offline_guard == ["dns", "connect"]
    offline_guard.clear()  # this test provoked the guard on purpose


def test_offline_guard_hides_real_groq_credentials_from_ordinary_tests(monkeypatch):
    # The fixture ran before this body; a key exported by a developer is gone.
    import os

    assert not [name for name in os.environ if name.startswith("GROQ_")]
    assert GroqProvider.from_env().availability.available is False


# --- operator docs stay in sync with the configuration ------------------------


def _env_example_assignments() -> tuple[dict[str, str], set[str]]:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    active: dict[str, str] = {}
    documented: set[str] = set()
    for line in text.splitlines():
        candidate = line.lstrip("# ").strip()
        name, sep, value = candidate.partition("=")
        if sep and name.replace("_", "").isalnum() and name.isupper():
            documented.add(name)
            if not line.startswith("#"):
                active[name] = value
    return active, documented


def test_env_example_documents_every_groq_variable_and_its_values_are_valid():
    active, documented = _env_example_assignments()

    assert {
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "GROQ_API_BASE_URL",
        "GROQ_REASONING_EFFORT",
        "GROQ_MAX_COMPLETION_TOKENS",
        "RUN_LIVE_GROQ",
    } <= documented
    assert "RUN_LIVE_GROQ" not in active, "the paid-test switch must stay commented out"
    example = GroqConfig.from_env({k: v for k, v in active.items() if k.startswith("GROQ_")})
    assert example == GroqConfig(), "the shipped example must equal the built-in defaults"
    assert example.has_credentials is False, "no key is shipped"
