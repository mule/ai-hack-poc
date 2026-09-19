"""Cerebras provider adapter: offline unit and contract tests.

Everything here runs without network or credentials: the transport is a fake
that records requests and replays canned Cerebras completions responses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import make_request, valid_room_dict

from dungeon_director import cerebras as cerebras_module
from dungeon_director.cerebras import (
    _ROOM_PLAN_JSON_SCHEMA,
    DEFAULT_API_BASE_URL,
    DEFAULT_CEREBRAS_MODEL,
    DEFAULT_MAX_COMPLETION_TOKENS,
    CerebrasConfig,
    CerebrasProvider,
    CerebrasTransportRequest,
    CerebrasTransportResponse,
    HttpxCerebrasTransport,
    build_prompt_messages,
)
from dungeon_director.contracts import (
    EnvironmentalTag,
    Exit,
    ExitDirection,
    ExitKind,
    GenerationRequest,
    RoomPlan,
    RoomSize,
    RoomType,
)
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry, default_registry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cerebras"
FAKE_KEY = "csk-test-secret-key-do-not-leak"


class FakeCerebrasTransport:
    """Records every request; replies with queued responses or raises."""

    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None) -> None:
        self.requests: list[CerebrasTransportRequest] = []
        self._responses = list(responses or [])
        self._error = error

    async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def http_response(
    body: Any, status: int = 200, headers: dict[str, str] | None = None
) -> CerebrasTransportResponse:
    if isinstance(body, (bytes, bytearray)):
        content = bytes(body)
    else:
        content = json.dumps(body).encode("utf-8")
    return CerebrasTransportResponse(status_code=status, headers=headers or {}, body=content)


def valid_room_json(
    *,
    room_id: str = "cerebras-test-room-1",
    depth: int = 3,
    direction: str = "south",
    kind: str = "door",
    locked: bool = False,
    room_type: str = "chamber",
    size: str = "medium",
    danger: int = 2,
    has_secret: bool = False,
    secret_probability: float = 0.0,
    **overrides: Any,
) -> str:
    room = {
        "room_id": room_id,
        "depth": depth,
        "room_type": room_type,
        "size": size,
        "danger": danger,
        "enemy_density": 0.25,
        "loot_density": 0.5,
        "secret_probability": secret_probability,
        "has_secret": has_secret,
        "exits": [{"direction": direction, "kind": kind, "locked": locked}],
        "environmental_tags": ["dark"],
        "description": "A quiet dark chamber.",
    }
    room.update(overrides)
    return json.dumps(room)


def cerebras_chat_completion(
    room_content: str | None = None,
    *,
    id: str = "chatcmpl-test-12345",
    model: str = DEFAULT_CEREBRAS_MODEL,
    finish_reason: str = "stop",
    prompt_tokens: int = 250,
    completion_tokens: int = 90,
    time_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = room_content if room_content is not None else valid_room_json()
    return {
        "id": id,
        "object": "chat.completion",
        "created": 1726750000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "time_info": time_info
        or {
            "queue_time": 0.001,
            "prompt_time": 0.004,
            "completion_time": 0.040,
            "total_time": 0.045,
        },
    }


def provider_with(
    transport: FakeCerebrasTransport,
    *,
    key: str = FAKE_KEY,
    model: str = DEFAULT_CEREBRAS_MODEL,
    base_url: str = DEFAULT_API_BASE_URL,
) -> CerebrasProvider:
    return CerebrasProvider(
        CerebrasConfig(model=model, api_base_url=base_url, api_key=key),
        transport,
    )


def generate(
    transport: FakeCerebrasTransport,
    request: Any = None,
    **provider_kwargs: Any,
) -> ProviderResult:
    provider = provider_with(transport, **provider_kwargs)
    return asyncio.run(provider.generate(request or make_request(), model=provider.default_model))


# --- identifiers, availability, configuration --------------------------------


def test_provider_has_stable_ids_and_configurable_model():
    provider = provider_with(FakeCerebrasTransport(), model="qwen-3-custom")

    assert provider.provider_id == "cerebras"
    assert provider.models == ("qwen-3-custom",)
    assert provider.default_model == "qwen-3-custom"


def test_config_defaults_and_env_reading(monkeypatch):
    for name in ("CEREBRAS_API_KEY", "CEREBRAS_MODEL", "CEREBRAS_API_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    empty = CerebrasConfig.from_env({})
    assert empty.model == DEFAULT_CEREBRAS_MODEL == "qwen-3.8-27b"
    assert empty.api_base_url == DEFAULT_API_BASE_URL == "https://api.cerebras.ai/v1"
    assert empty.has_credentials is False
    assert empty.max_completion_tokens == DEFAULT_MAX_COMPLETION_TOKENS

    configured = CerebrasConfig.from_env(
        {
            "CEREBRAS_API_KEY": "test-key",
            "CEREBRAS_MODEL": "qwen-3.8-custom",
            "CEREBRAS_API_BASE_URL": "https://custom.cerebras.proxy/v1/",
            "CEREBRAS_MAX_COMPLETION_TOKENS": "256",
        }
    )
    assert configured.max_completion_tokens == 256
    assert configured.has_credentials is True
    assert configured.model == "qwen-3.8-custom"
    assert configured.completions_url == "https://custom.cerebras.proxy/v1/chat/completions"


def test_availability_reports_missing_configuration_by_variable_name(monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    provider = CerebrasProvider.from_env({})

    availability = provider.availability
    assert availability.available is False
    assert "CEREBRAS_API_KEY" in availability.reason
    assert provider_with(FakeCerebrasTransport()).availability.available is True


def test_credentials_never_appear_in_repr():
    provider = provider_with(FakeCerebrasTransport())
    config = provider._config

    text = f"{provider!r} {config!r}"
    assert FAKE_KEY not in text
    assert "configured=True" in repr(provider)


def test_generate_without_credentials_is_a_classified_error():
    provider = CerebrasProvider(CerebrasConfig(), FakeCerebrasTransport())

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model=provider.default_model))
    assert info.value.code is ErrorKind.PROVIDER_ERROR


@pytest.mark.parametrize(
    "url",
    [
        "ftp://api.cerebras.ai/v1",
        "http://remote.cerebras.ai/v1",  # plaintext to a non-loopback host
        "http://localhost.evil.example/v1",  # loopback-looking, not loopback
        "http://127.0.0.1.evil.example/v1",
        "https://user:pw@api.cerebras.ai/v1",  # credentials belong in the header
        "https://api.cerebras.ai/v1?api_key=x",
        "https://api.cerebras.ai/v1#frag",
        "https://api.cerebras.ai:notaport/v1",
        "https:///v1",
        "api.cerebras.ai/v1",
        "",
    ],
)
def test_unsafe_or_malformed_api_base_url_is_a_config_error(url: str):
    with pytest.raises(DirectorConfigError) as info:
        CerebrasConfig(api_base_url=url)

    assert "pw@" not in str(info.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.cerebras.ai/v1",
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://[::1]:8000/v1",
        "http://127.0.0.2/v1",
    ],
)
def test_https_and_loopback_http_base_urls_are_accepted(url: str):
    assert CerebrasConfig(api_base_url=url).api_base_url == url


@pytest.mark.parametrize("key", ["has space", "new\nline", "tab\tkey", "ключ", "k" * 4097])
def test_malformed_api_key_is_a_config_error_that_never_echoes_it(key: str):
    with pytest.raises(DirectorConfigError) as info:
        CerebrasConfig(api_key=key)

    assert key.strip() not in str(info.value)


@pytest.mark.parametrize("model", ["", " ", "has space", "bad\nmodel", "-leading", "x" * 129])
def test_invalid_model_id_is_a_config_error(model: str):
    with pytest.raises(DirectorConfigError):
        CerebrasConfig(model=model)


@pytest.mark.parametrize("tokens", ["0", "63", "2049", "-5", "abc", "1.5", "1e3"])
def test_max_completion_tokens_outside_the_cap_is_a_config_error(tokens: str):
    with pytest.raises(DirectorConfigError):
        CerebrasConfig.from_env({"CEREBRAS_MAX_COMPLETION_TOKENS": tokens})


# --- request shape and output generation -------------------------------------


def test_generate_sends_schema_and_reasoning_effort():
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion())])
    req = make_request()

    result = generate(transport, request=req)

    assert len(transport.requests) == 1
    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.url == "https://api.cerebras.ai/v1/chat/completions"
    assert sent.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert sent.headers["Content-Type"] == "application/json"

    body = sent.json_body
    assert body["model"] == DEFAULT_CEREBRAS_MODEL
    assert body["reasoning_effort"] == "none"
    assert body["response_format"]["type"] == "json_schema"
    schema_wrapper = body["response_format"]["json_schema"]
    assert schema_wrapper["strict"] is True
    assert schema_wrapper["name"] == "room_plan"
    assert schema_wrapper["schema"]["type"] == "object"
    assert schema_wrapper["schema"]["additionalProperties"] is False

    assert body["max_completion_tokens"] == DEFAULT_MAX_COMPLETION_TOKENS
    assert "max_tokens" not in body
    assert body["temperature"] == 0
    assert isinstance(body["seed"], int)
    assert set(body) == {
        "model",
        "messages",
        "response_format",
        "reasoning_effort",
        "max_completion_tokens",
        "temperature",
        "seed",
    }

    # The adapter hands the raw model text to the service; it never validates it.
    assert isinstance(result.payload, str)
    assert json.loads(result.payload)["room_id"] == "cerebras-test-room-1"
    assert RoomPlan.model_validate_json(result.payload).depth == req.state.depth


def test_configured_completion_token_cap_is_sent():
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion())])
    provider = CerebrasProvider(
        CerebrasConfig(api_key=FAKE_KEY, max_completion_tokens=128), transport
    )

    asyncio.run(provider.generate(make_request(), model=provider.default_model))

    assert transport.requests[0].json_body["max_completion_tokens"] == 128


def test_identical_requests_produce_identical_request_bodies():
    bodies = []
    for _ in range(2):
        transport = FakeCerebrasTransport([http_response(cerebras_chat_completion())])
        generate(transport, request=make_request())
        bodies.append(json.dumps(transport.requests[0].json_body, sort_keys=True))

    assert bodies[0] == bodies[1]


def test_generate_rejects_a_model_the_provider_was_not_configured_with():
    transport = FakeCerebrasTransport()
    provider = provider_with(transport)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model="some-other-model"))

    assert info.value.code is ErrorKind.PROVIDER_ERROR
    assert transport.requests == []


def test_sample_request_fixture_matches_what_the_adapter_sends():
    contracts = FIXTURES.parents[3] / "contracts" / "fixtures" / "generation_request.json"
    request = GenerationRequest.model_validate_json(contracts.read_text())
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion())])

    generate(transport, request=request)

    expected = json.loads((FIXTURES / "sample_request.json").read_text())
    assert transport.requests[0].json_body == expected
    assert FAKE_KEY not in json.dumps(expected)


def test_sample_response_fixture_yields_a_valid_room_through_the_service():
    contracts = FIXTURES.parents[3] / "contracts" / "fixtures" / "generation_request.json"
    request = GenerationRequest.model_validate_json(contracts.read_text())
    sample = json.loads((FIXTURES / "sample_response.json").read_text())
    service = cerebras_service(FakeCerebrasTransport([http_response(sample)]))

    outcome = asyncio.run(service.generate(request))

    assert outcome.status_code == 200
    assert outcome.response.room is not None
    assert outcome.response.metadata.usage.input_tokens == 412


def test_prompt_messages_are_compact_and_carry_the_required_backlink():
    request = make_request()

    system, user = build_prompt_messages(request)

    assert system["role"] == "system" and user["role"] == "user"
    assert "connecting back" in user["content"]
    assert len(json.dumps([system, user])) < 3000


# --- strict schema -------------------------------------------------------------


def _objects_in(node: Any):
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield node
        for value in node.values():
            yield from _objects_in(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects_in(value)


def test_schema_is_strict_mode_compatible():
    objects = list(_objects_in(_ROOM_PLAN_JSON_SCHEMA))

    assert len(objects) == 2  # the room and its exits
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_schema_properties_and_enums_cannot_drift_from_the_contract():
    props = _ROOM_PLAN_JSON_SCHEMA["properties"]
    exit_props = props["exits"]["items"]["properties"]
    assert set(props) == set(RoomPlan.model_fields)
    assert set(exit_props) == set(Exit.model_fields)

    for name, members in [
        ("room_type", RoomType),
        ("size", RoomSize),
    ]:
        assert props[name]["enum"] == [m.value for m in members]
    assert exit_props["direction"]["enum"] == [m.value for m in ExitDirection]
    assert exit_props["kind"]["enum"] == [m.value for m in ExitKind]
    assert props["environmental_tags"]["items"]["enum"] == [m.value for m in EnvironmentalTag]


def test_every_schema_enum_value_is_accepted_by_the_real_contract():
    base = valid_room_dict(make_request())
    props = _ROOM_PLAN_JSON_SCHEMA["properties"]
    exit_props = props["exits"]["items"]["properties"]

    for value in props["room_type"]["enum"]:
        RoomPlan.model_validate({**base, "room_type": value})
    for value in props["size"]["enum"]:
        RoomPlan.model_validate({**base, "size": value})
    for direction in exit_props["direction"]["enum"]:
        for kind in exit_props["kind"]["enum"]:
            exits = [{"direction": direction, "kind": kind, "locked": True}]
            RoomPlan.model_validate({**base, "exits": exits})
    for tag in props["environmental_tags"]["items"]["enum"]:
        RoomPlan.model_validate({**base, "environmental_tags": [tag]})


def test_usage_and_provider_metadata_are_captured():
    transport = FakeCerebrasTransport(
        [
            http_response(
                cerebras_chat_completion(
                    id="chatcmpl-id-999",
                    finish_reason="stop",
                    prompt_tokens=312,
                    completion_tokens=78,
                )
            )
        ]
    )

    result = generate(transport)

    assert result.usage is not None
    assert result.usage.input_tokens == 312
    assert result.usage.output_tokens == 78
    assert result.usage.estimated_cost_usd is None

    meta = result.provider_metadata
    assert meta["cerebras_id"] == "chatcmpl-id-999"
    assert meta["cerebras_model"] == DEFAULT_CEREBRAS_MODEL
    assert meta["finish_reason"] == "stop"
    assert meta["prompt_tokens"] == 312
    assert meta["completion_tokens"] == 78
    assert meta["upstream_http_status"] == 200
    assert "adapter_elapsed_ms" in meta
    assert "time_total_time" in meta

    serialized = json.dumps(meta)
    assert len(serialized.encode("utf-8")) <= 8192
    assert len(meta) <= 32
    assert FAKE_KEY not in serialized


def test_aclose_leaves_an_injected_transport_alone():
    class Closable(FakeCerebrasTransport):
        closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    injected = Closable()
    provider = CerebrasProvider(CerebrasConfig(api_key=FAKE_KEY), injected)

    asyncio.run(provider.aclose())

    assert injected.closed == 0
    assert provider._transport is injected


def test_aclose_closes_the_lazily_created_transport_once_and_recreates_on_reuse(monkeypatch):
    created: list[Any] = []

    class SpyTransport(FakeCerebrasTransport):
        def __init__(self) -> None:
            super().__init__([http_response(cerebras_chat_completion())])
            self.closed = 0
            created.append(self)

        async def aclose(self) -> None:
            self.closed += 1

    monkeypatch.setattr(cerebras_module, "HttpxCerebrasTransport", SpyTransport)
    provider = CerebrasProvider(CerebrasConfig(api_key=FAKE_KEY))

    asyncio.run(provider.generate(make_request(), model=provider.default_model))
    assert len(created) == 1
    asyncio.run(provider.aclose())
    asyncio.run(provider.aclose())  # idempotent

    assert created[0].closed == 1
    assert provider._transport is None


def test_httpx_transport_aclose_closes_the_client():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    transport = HttpxCerebrasTransport(client)

    asyncio.run(transport.aclose())

    assert client.is_closed


# --- error handling and classification ---------------------------------------


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, ErrorKind.PROVIDER_ERROR),
        (403, ErrorKind.PROVIDER_ERROR),
        (404, ErrorKind.PROVIDER_ERROR),
        (422, ErrorKind.PROVIDER_ERROR),
        (429, ErrorKind.RATE_LIMITED),
        (500, ErrorKind.PROVIDER_ERROR),
        (503, ErrorKind.PROVIDER_ERROR),
    ],
)
def test_http_error_statuses_are_mapped(status: int, expected_code: ErrorKind):
    resp = http_response({"error": "some upstream error"}, status=status)
    transport = FakeCerebrasTransport([resp])

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is expected_code
    assert FAKE_KEY not in exc_info.value.message


def test_invalid_json_body_is_classified():
    transport = FakeCerebrasTransport([CerebrasTransportResponse(200, {}, b"not json")])

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.INVALID_JSON


def test_empty_choices_raises_empty_response():
    transport = FakeCerebrasTransport([http_response({"choices": []})])

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.EMPTY_RESPONSE


def test_empty_content_raises_empty_response():
    resp = cerebras_chat_completion("")
    transport = FakeCerebrasTransport([http_response(resp)])

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.EMPTY_RESPONSE


def test_provider_does_not_validate_the_plan_itself():
    """Canonical RoomPlan validation belongs to the service (see the service tests below)."""
    garbage = '{"room_id": 123, "depth": "not an int"}'
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion(garbage))])

    result = generate(transport)

    assert result.payload == garbage
    assert result.usage is not None


def test_truncated_completion_is_passed_on_with_its_finish_reason():
    transport = FakeCerebrasTransport(
        [http_response(cerebras_chat_completion('{"room_id": "cut-off', finish_reason="length"))]
    )

    result = generate(transport)

    assert result.payload == '{"room_id": "cut-off'
    assert result.provider_metadata["finish_reason"] == "length"


def test_non_object_success_body_is_a_schema_violation():
    transport = FakeCerebrasTransport([http_response([1, 2, 3])])

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.SCHEMA_VIOLATION


def test_redirect_status_is_an_error_and_is_not_followed():
    transport = FakeCerebrasTransport(
        [http_response(b"", status=307, headers={"location": "https://evil.example/"})]
    )

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.PROVIDER_ERROR
    assert len(transport.requests) == 1


def test_hostile_time_info_cannot_bloat_or_poison_the_metadata():
    hostile = {
        "total_time": 0.5,
        "x" * 200: 1.0,
        "Bad Key": 1.0,
        "nan": float("nan"),
        "inf": float("inf"),
        "flag": True,
        "text": "not a number",
        **{f"k{i}": float(i) for i in range(100)},
    }
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion(time_info=hostile))])

    meta = generate(transport).provider_metadata

    assert meta["time_total_time"] == 0.5
    assert len(meta) <= 32
    assert len(json.dumps(meta).encode()) <= 8192
    assert not any(key in meta for key in ("time_nan", "time_inf", "time_flag", "time_text"))
    assert all(len(key) <= 64 for key in meta)


def test_cancellation_propagates():
    transport = FakeCerebrasTransport(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        generate(transport)


def test_transport_timeout_is_classified():
    import httpx

    transport = FakeCerebrasTransport(error=httpx.ConnectTimeout("timeout"))

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.PROVIDER_TIMEOUT


def test_unexpected_transport_exception_is_classified_without_its_text():
    transport = FakeCerebrasTransport(error=RuntimeError(f"boom {FAKE_KEY} https://secret.example"))

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.PROVIDER_ERROR
    assert FAKE_KEY not in exc_info.value.message
    assert "secret.example" not in exc_info.value.message


def test_httpx_network_error_message_does_not_carry_exception_text():
    transport = FakeCerebrasTransport(error=httpx.ConnectError(f"cannot reach {FAKE_KEY}"))

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.PROVIDER_ERROR
    assert FAKE_KEY not in exc_info.value.message


# --- real httpx transport (in-process, no network) ------------------------------


def _httpx_send(handler, *, body_limit_probe: bool = False) -> CerebrasTransportResponse:
    async def run() -> CerebrasTransportResponse:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        transport = HttpxCerebrasTransport(client)
        try:
            return await transport.send(
                CerebrasTransportRequest(
                    method="POST",
                    url="https://api.cerebras.ai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {FAKE_KEY}"},
                    json_body={"hello": "world"},
                )
            )
        finally:
            await transport.aclose()

    return asyncio.run(run())


def test_httpx_transport_returns_status_headers_and_body():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    response = _httpx_send(handler)

    assert response.status_code == 200
    assert json.loads(response.body) == {"ok": True}
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert json.loads(seen[0].content) == {"hello": "world"}


def test_httpx_transport_accepts_a_body_at_the_one_mebibyte_cap():
    payload = b"x" * 1_048_576

    response = _httpx_send(lambda request: httpx.Response(200, content=payload))

    assert len(response.body) == 1_048_576


def test_httpx_transport_rejects_a_body_over_the_cap_while_streaming():
    def handler(request: httpx.Request) -> httpx.Response:
        async def chunks():
            for _ in range(20):  # 20 x 64 KiB = 1.25 MiB, no content-length header
                yield b"x" * 65_536

        return httpx.Response(200, content=chunks())

    with pytest.raises(cerebras_module.CerebrasResponseTooLarge):
        _httpx_send(handler)


def test_httpx_transport_rejects_an_oversized_declared_content_length_early():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": "5000000"}, content=b"x")

    with pytest.raises((cerebras_module.CerebrasResponseTooLarge, httpx.HTTPError)):
        _httpx_send(handler)


def test_oversized_response_is_a_classified_provider_error_without_details():
    error = cerebras_module.CerebrasResponseTooLarge()
    transport = FakeCerebrasTransport(error=error)

    with pytest.raises(ProviderError) as exc_info:
        generate(transport)

    assert exc_info.value.code is ErrorKind.PROVIDER_ERROR
    assert "limit" in exc_info.value.message


def test_httpx_transport_does_not_follow_redirects():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example/steal"})

    response = _httpx_send(handler)

    assert response.status_code == 307
    assert calls == ["https://api.cerebras.ai/v1/chat/completions"]


# --- registry and service integration -----------------------------------------


def test_registry_selects_cerebras_provider():
    registry = ProviderRegistry()
    registry.register(provider_with(FakeCerebrasTransport()))

    selection = registry.select("cerebras", None)
    assert selection.model == DEFAULT_CEREBRAS_MODEL
    assert selection.provider.provider_id == "cerebras"


def test_selecting_unconfigured_cerebras_reports_unavailable_through_the_service(monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)

    service = DirectorService(default_registry(), DirectorSettings())
    outcome = asyncio.run(service.generate(make_request(), provider="cerebras"))

    assert outcome.status_code == 503
    assert outcome.response.success is False
    assert outcome.response.metadata.error is not None
    assert outcome.response.metadata.provider == "cerebras"
    assert outcome.response.metadata.provider_metadata == {
        "selection_error": "provider_unavailable"
    }


def test_service_successful_generation_with_cerebras(monkeypatch):
    transport = FakeCerebrasTransport([http_response(cerebras_chat_completion())])
    provider = provider_with(transport)

    registry = ProviderRegistry()
    registry.register(provider)
    settings = DirectorSettings(default_provider="cerebras", default_model=DEFAULT_CEREBRAS_MODEL)
    service = DirectorService(registry, settings)

    req = make_request()
    outcome = asyncio.run(service.generate(req))

    assert outcome.status_code == 200
    assert outcome.response.success is True
    assert outcome.response.room is not None
    assert outcome.response.metadata.provider == "cerebras"
    assert outcome.response.metadata.model == DEFAULT_CEREBRAS_MODEL
    assert outcome.response.room.room_id == "cerebras-test-room-1"


def cerebras_service(transport: FakeCerebrasTransport, *, timeout: float = 5.0) -> DirectorService:
    registry = ProviderRegistry()
    registry.register(provider_with(transport))
    settings = DirectorSettings(
        default_provider="cerebras",
        default_model=DEFAULT_CEREBRAS_MODEL,
        timeout_seconds=timeout,
    )
    return DirectorService(registry, settings)


def test_service_reports_usage_and_cerebras_metadata_on_success():
    service = cerebras_service(FakeCerebrasTransport([http_response(cerebras_chat_completion())]))

    outcome = asyncio.run(service.generate(make_request()))

    metadata = outcome.response.metadata
    assert outcome.status_code == 200
    assert metadata.usage is not None and metadata.usage.input_tokens == 250
    assert metadata.provider_metadata["cerebras_id"] == "chatcmpl-test-12345"


@pytest.mark.parametrize(
    ("content", "finish_reason", "code"),
    [
        ('{"room_id": 123, "depth": "not an int"}', "stop", ErrorKind.SCHEMA_VIOLATION),
        ('{"room_id": "cut-off', "length", ErrorKind.INVALID_JSON),
        ("I cannot help with that.", "stop", ErrorKind.INVALID_JSON),
        (
            valid_room_json(has_secret=True, secret_probability=0.0),
            "stop",
            ErrorKind.SCHEMA_VIOLATION,
        ),
        (valid_room_json(depth=99), "stop", ErrorKind.SCHEMA_VIOLATION),
    ],
    ids=["wrong_types", "truncated_by_token_cap", "prose", "semantic_invariant", "wrong_depth"],
)
def test_service_rejects_bad_model_output_but_keeps_usage_and_provider_metadata(
    content: str, finish_reason: str, code: ErrorKind
):
    """Regression: schema failures used to lose usage/metadata (adapter validated the plan)."""
    completion = cerebras_chat_completion(
        content,
        id="chatcmpl-bad-1",
        finish_reason=finish_reason,
        prompt_tokens=411,
        completion_tokens=77,
    )
    service = cerebras_service(FakeCerebrasTransport([http_response(completion)]))

    outcome = asyncio.run(service.generate(make_request()))

    response = outcome.response
    assert outcome.status_code == 502
    assert response.success is False and response.room is None
    assert response.metadata.error.code is code
    assert response.metadata.provider == "cerebras"
    assert response.metadata.usage is not None
    assert (response.metadata.usage.input_tokens, response.metadata.usage.output_tokens) == (
        411,
        77,
    )
    assert response.metadata.provider_metadata["cerebras_id"] == "chatcmpl-bad-1"
    assert response.metadata.provider_metadata["finish_reason"] == finish_reason
    assert response.metadata.provider_metadata["prompt_tokens"] == 411


def test_upstream_failure_response_never_contains_the_credential_or_upstream_text(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = {"error": {"message": f"bad key {FAKE_KEY}", "url": "https://secret.example"}}
    service = cerebras_service(FakeCerebrasTransport([http_response(leaky, status=401)]))

    outcome = asyncio.run(service.generate(make_request()))

    dumped = outcome.response.model_dump_json()
    assert outcome.status_code == 502
    for text in (dumped, caplog.text):
        assert FAKE_KEY not in text
        assert "secret.example" not in text


def test_transport_exception_text_is_kept_out_of_response_and_logs(caplog):
    caplog.set_level(logging.DEBUG)
    transport = FakeCerebrasTransport(error=httpx.ConnectError(f"dial failed for {FAKE_KEY}"))
    service = cerebras_service(transport)

    outcome = asyncio.run(service.generate(make_request()))

    assert outcome.status_code == 502
    assert FAKE_KEY not in outcome.response.model_dump_json()
    assert FAKE_KEY not in caplog.text


def test_upstream_rate_limit_maps_to_429_through_the_service():
    service = cerebras_service(FakeCerebrasTransport([http_response({}, status=429)]))

    outcome = asyncio.run(service.generate(make_request()))

    assert outcome.status_code == 429


def test_service_deadline_cancels_the_in_flight_cerebras_call():
    class HangingTransport(FakeCerebrasTransport):
        cancelled = False

        async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse:
            self.requests.append(request)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("unreachable")

    transport = HangingTransport()
    service = cerebras_service(transport, timeout=0.05)

    outcome = asyncio.run(service.generate(make_request()))

    assert outcome.status_code == 504
    assert transport.cancelled is True
    assert len(transport.requests) == 1, "a timeout must not retry"


def test_cancelling_the_request_task_cancels_the_cerebras_call_and_propagates():
    started = asyncio.Event
    state = {"cancelled": False}

    class HangingTransport(FakeCerebrasTransport):
        async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse:
            state["started"].set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                state["cancelled"] = True
                raise
            raise AssertionError("unreachable")

    async def scenario() -> None:
        state["started"] = started()
        service = cerebras_service(HangingTransport())
        task = asyncio.create_task(service.generate(make_request()))
        await state["started"].wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert state["cancelled"] is True


# --- default registry: opt-in credentials and safe fallback ---------------------


def _clear_cerebras_env(monkeypatch) -> None:
    for name in (
        "CEREBRAS_API_KEY",
        "CEREBRAS_MODEL",
        "CEREBRAS_API_BASE_URL",
        "CEREBRAS_MAX_COMPLETION_TOKENS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_registry_lists_cerebras_available_when_a_key_is_present(monkeypatch):
    _clear_cerebras_env(monkeypatch)
    monkeypatch.setenv("CEREBRAS_API_KEY", FAKE_KEY)

    described = {item.id: item for item in default_registry().describe()}

    assert described["cerebras"].available is True
    assert described["cerebras"].default_model == "qwen-3.8-27b"
    assert FAKE_KEY not in repr(described)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CEREBRAS_API_BASE_URL", "http://remote.example/v1"),
        ("CEREBRAS_API_BASE_URL", "https://user:hunter2@api.cerebras.ai/v1"),
        ("CEREBRAS_MODEL", "bad model id"),
        ("CEREBRAS_MAX_COMPLETION_TOKENS", "999999"),
        ("CEREBRAS_MAX_COMPLETION_TOKENS", "many"),
    ],
)
def test_invalid_cerebras_environment_disables_only_cerebras_and_logs_no_values(
    monkeypatch, caplog, name, value
):
    _clear_cerebras_env(monkeypatch)
    monkeypatch.setenv("CEREBRAS_API_KEY", FAKE_KEY)
    monkeypatch.setenv(name, value)
    caplog.set_level(logging.DEBUG)

    registry = default_registry()

    described = {item.id: item for item in registry.describe()}
    assert registry.select("rules-baseline", None).model == "builtin-v1"
    assert described["cerebras"].available is False
    logged = caplog.text
    assert FAKE_KEY not in logged
    assert value not in logged
    assert "hunter2" not in logged


def test_a_default_provider_of_cerebras_without_credentials_stops_startup(monkeypatch):
    _clear_cerebras_env(monkeypatch)

    with pytest.raises(DirectorConfigError):
        DirectorService(default_registry(), DirectorSettings(default_provider="cerebras"))


def test_registry_aclose_closes_the_cerebras_transport_it_created(monkeypatch):
    closed: list[bool] = []

    class SpyTransport(FakeCerebrasTransport):
        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(cerebras_module, "HttpxCerebrasTransport", SpyTransport)
    provider = CerebrasProvider(CerebrasConfig(api_key=FAKE_KEY))
    provider._transport = SpyTransport()  # what a first generate() call would have created
    registry = ProviderRegistry()
    registry.register(provider)

    asyncio.run(registry.aclose())

    assert closed == [True]
