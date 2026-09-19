"""Cloudflare TypeSafe Jev adapter: offline unit and contract tests.

Everything here runs without network or credentials: the transport is a fake
that records requests and replays canned Jev answers. The response samples
follow the documented shapes (see director/docs/cloudflare-jev.md).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import make_request, request_for_exit, request_payload

from dungeon_director.cloudflare_jev import (
    DEFAULT_JEV_MODEL,
    CloudflareJevProvider,
    HttpxJevTransport,
    JevConfig,
    JevTransportRequest,
    JevTransportResponse,
)
from dungeon_director.contracts import (
    EnvironmentalTag,
    ExitKind,
    GenerationResponse,
    RoomPlan,
    RoomType,
)
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jev"
TAG_NAMES = [tag.value for tag in EnvironmentalTag]
FAKE_ACCOUNT = "acct-test-1234"
FAKE_TOKEN = "cf-secret-token-do-not-print"

TAG_BASE_PROBABILITY = 0.1


class FakeTransport:
    """Records every request; replies with queued responses or raises."""

    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None) -> None:
        self.requests: list[JevTransportRequest] = []
        self._responses = list(responses or [])
        self._error = error

    async def send(self, request: JevTransportRequest) -> JevTransportResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def http_response(
    body: Any, status: int = 200, headers: dict[str, str] | None = None
) -> JevTransportResponse:
    if isinstance(body, (bytes, bytearray)):
        content = bytes(body)
    else:
        content = json.dumps(body).encode("utf-8")
    return JevTransportResponse(status_code=status, headers=headers or {}, body=content)


def envelope(
    payload: Any, *, success: bool = True, errors: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "result": payload if success else None,
        "success": success,
        "errors": errors if errors is not None else [],
        "messages": [],
    }


def jev_answers(**overrides: Any) -> dict[str, Any]:
    answers: dict[str, Any] = {
        "room_type": {
            "type": "choice",
            "choice": "chamber",
            "confidence": 0.78,
            "probabilities": {"chamber": 0.56, "room": 0.21, "shrine": 0.09, "corridor": 0.05},
        },
        "size": {
            "type": "choice",
            "choice": "medium",
            "confidence": 0.91,
            "probabilities": {"medium": 0.82, "small": 0.1, "large": 0.05},
        },
        "danger": {
            "type": "score",
            "score": 2.2,
            "confidence": 0.84,
            "legend": {"0": "d1", "1": "d2", "2": "d3", "3": "d4", "4": "d5"},
            "probabilities": {"2": 0.67, "3": 0.15},
        },
        "enemy_density": {
            "type": "score",
            "score": 1.0,
            "confidence": 0.88,
            "legend": {"0": "e0", "1": "e1", "2": "e2", "3": "e3", "4": "e4"},
            "probabilities": {"1": 0.61},
        },
        "loot_density": {
            "type": "score",
            "score": 2.0,
            "confidence": 0.79,
            "legend": {"0": "l0", "1": "l1", "2": "l2", "3": "l3", "4": "l4"},
            "probabilities": {"2": 0.6},
        },
        "has_secret": {"type": "noul", "noul": 0.61},
        "exit_count": {
            "type": "choice",
            "choice": "1",
            "confidence": 0.73,
            "probabilities": {"1": 0.68, "0": 0.14, "2": 0.16, "3": 0.02},
        },
    }
    answers.update(
        {f"tag_{name}": {"type": "noul", "noul": TAG_BASE_PROBABILITY} for name in TAG_NAMES}
    )
    answers["tag_dark"] = {"type": "noul", "noul": 0.72}
    answers.update(overrides)
    return answers


def jev_payload(answers: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": jev_answers() if answers is None else answers,
        "usage": {"input_tokens": 642, "output_tokens": 118},
    }


def provider_with(
    transport: FakeTransport,
    *,
    account: str = FAKE_ACCOUNT,
    token: str = FAKE_TOKEN,
    model: str = DEFAULT_JEV_MODEL,
    base_url: str = "https://api.cloudflare.example/client/v4",
) -> CloudflareJevProvider:
    return CloudflareJevProvider(
        JevConfig(model=model, api_base_url=base_url, account_id=account, api_token=token),
        transport,
    )


def generate(
    transport: FakeTransport,
    request: Any = None,
    **provider_kwargs: Any,
) -> ProviderResult:
    provider = provider_with(transport, **provider_kwargs)
    return asyncio.run(provider.generate(request or make_request(), model=provider.default_model))


def provider_error_from(excinfo: pytest.ExceptionInfo[ProviderError]) -> ProviderError:
    return excinfo.value


# --- identifiers, availability, credential hygiene ---------------------------


def test_provider_has_stable_ids_and_configurable_model():
    provider = provider_with(FakeTransport(), model="typesafe/jev-2")

    assert provider.provider_id == "cloudflare-jev"
    assert provider.models == ("typesafe/jev-2",)
    assert provider.default_model == "typesafe/jev-2"


def test_config_defaults_and_env_reading(monkeypatch):
    for name in (
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_JEV_MODEL",
        "CLOUDFLARE_JEV_API_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    empty = JevConfig.from_env({})

    assert empty.model == DEFAULT_JEV_MODEL == "typesafe/jev"
    assert empty.api_base_url == "https://api.cloudflare.com/client/v4"
    assert empty.has_credentials is False

    configured = JevConfig.from_env(
        {
            "CLOUDFLARE_ACCOUNT_ID": "acct",
            "CLOUDFLARE_API_TOKEN": "tok",
            "CLOUDFLARE_JEV_MODEL": "typesafe/jev-2",
            "CLOUDFLARE_JEV_API_BASE_URL": "https://proxy.example/client/v4/",
        }
    )

    assert configured.has_credentials is True
    assert configured.model == "typesafe/jev-2"
    assert configured.run_url == "https://proxy.example/client/v4/accounts/acct/ai/run"


def test_availability_reports_missing_configuration_by_variable_name(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    provider = CloudflareJevProvider.from_env({})

    availability = provider.availability

    assert availability.available is False
    assert "CLOUDFLARE_ACCOUNT_ID" in availability.reason
    assert "CLOUDFLARE_API_TOKEN" in availability.reason
    assert provider_with(FakeTransport()).availability.available is True


def test_availability_names_only_the_missing_variable(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    provider = CloudflareJevProvider.from_env({"CLOUDFLARE_API_TOKEN": "tok"})

    availability = provider.availability

    assert availability.available is False
    assert "CLOUDFLARE_ACCOUNT_ID" in availability.reason
    assert "CLOUDFLARE_API_TOKEN not" not in availability.reason.replace(
        "CLOUDFLARE_ACCOUNT_ID", ""
    )


def test_credentials_never_appear_in_reprs():
    provider = provider_with(FakeTransport())
    config = provider._config

    text = f"{provider!r} {config!r}"

    assert FAKE_TOKEN not in text and FAKE_ACCOUNT not in text
    assert "configured=True" in repr(provider)


def test_generate_without_credentials_is_a_classified_error():
    provider = CloudflareJevProvider(JevConfig(), FakeTransport())

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model=DEFAULT_JEV_MODEL))

    assert info.value.code is ErrorKind.PROVIDER_ERROR
    assert provider._config.has_credentials is False


@pytest.mark.parametrize(
    "account_id", ["acct/../../etc", "acct with space", "acct?token=x", "acct#frag"]
)
def test_account_id_is_charset_validated_before_it_reaches_a_url(account_id):
    with pytest.raises(DirectorConfigError):
        JevConfig(account_id=account_id, api_token="t")


def test_base_url_must_be_http_s():
    with pytest.raises(DirectorConfigError):
        JevConfig(account_id="acct", api_token="t", api_base_url="ftp://cloudflare.example")


@pytest.mark.parametrize(
    "base_url",
    ["http://api.cloudflare.example/client/v4", "https://", "api.cloudflare.example"],
)
def test_base_url_requires_https_for_remote_hosts_and_an_absolute_host(base_url):
    with pytest.raises(DirectorConfigError):
        JevConfig(account_id="acct", api_token="t", api_base_url=base_url)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_http_base_url_is_allowed_for_local_integration_tests(host):
    config = JevConfig(account_id="acct", api_token="t", api_base_url=f"http://{host}:8080")

    assert config.run_url.endswith("/accounts/acct/ai/run")


def test_empty_configuration_stays_valid_so_the_provider_can_register_unavailable():
    assert JevConfig().has_credentials is False


@pytest.mark.parametrize(
    "account_id", ["a", "023f1b8c4d5e6f7a8b9c0d1e2f3a4b5c", "acct-test-1234", "x" * 129]
)
def test_valid_account_ids_are_accepted(account_id):
    assert JevConfig(account_id=account_id, api_token="t").has_credentials is True


def test_from_env_rejects_injected_account_ids():
    with pytest.raises(DirectorConfigError):
        JevConfig.from_env(
            {"CLOUDFLARE_ACCOUNT_ID": "acct/../../etc", "CLOUDFLARE_API_TOKEN": "tok"}
        )


class CountingTransport:
    """Stands in for HttpxJevTransport: counts creations, sends and closes."""

    instances: list[CountingTransport] = []

    def __init__(self) -> None:
        self.sent = 0
        self.closed = False
        CountingTransport.instances.append(self)

    async def send(self, request: JevTransportRequest) -> JevTransportResponse:
        self.sent += 1
        return http_response(envelope(jev_payload()))

    async def aclose(self) -> None:
        self.closed = True


def test_default_transport_is_created_once_and_reused(monkeypatch):
    import dungeon_director.cloudflare_jev as cloudflare_jev

    CountingTransport.instances = []
    monkeypatch.setattr(cloudflare_jev, "HttpxJevTransport", CountingTransport)
    provider = CloudflareJevProvider(
        JevConfig(api_base_url="https://api.cloudflare.example", account_id="a", api_token="t")
    )

    asyncio.run(provider.generate(make_request(), model=provider.default_model))
    asyncio.run(provider.generate(make_request(), model=provider.default_model))

    assert len(CountingTransport.instances) == 1, "one shared client, not one per call"
    assert CountingTransport.instances[0].sent == 2


def test_real_httpx_transport_maps_request_and_suppresses_sensitive_url_logs(caplog):
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=envelope(jev_payload()), headers={"cf-ray": "ray-test"})

    async def exercise() -> ProviderResult:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpxJevTransport(client)
        provider = CloudflareJevProvider(
            JevConfig(account_id=FAKE_ACCOUNT, api_token=FAKE_TOKEN), transport
        )
        try:
            return await provider.generate(make_request(), model=provider.default_model)
        finally:
            await client.aclose()

    with caplog.at_level("INFO"):
        result = asyncio.run(exercise())

    assert isinstance(result.payload, RoomPlan)
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/client/v4/accounts/{FAKE_ACCOUNT}/ai/run"
    assert seen[0].headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert json.loads(seen[0].content)["model"] == DEFAULT_JEV_MODEL
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_ACCOUNT not in logged and FAKE_TOKEN not in logged


def test_real_httpx_transport_caps_streamed_response_bodies():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1_048_577)

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = HttpxJevTransport(client)
        provider = CloudflareJevProvider(
            JevConfig(account_id=FAKE_ACCOUNT, api_token=FAKE_TOKEN), transport
        )
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
            return httpx.Response(200, json=envelope(jev_payload()))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = CloudflareJevProvider(
            JevConfig(account_id=FAKE_ACCOUNT, api_token=FAKE_TOKEN),
            HttpxJevTransport(client),
        )
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


def test_aclose_closes_an_owned_transport_and_leaves_injected_transports_alone(monkeypatch):
    import dungeon_director.cloudflare_jev as cloudflare_jev

    CountingTransport.instances = []
    monkeypatch.setattr(cloudflare_jev, "HttpxJevTransport", CountingTransport)
    provider = CloudflareJevProvider(
        JevConfig(api_base_url="https://api.cloudflare.example", account_id="a", api_token="t")
    )
    asyncio.run(provider.generate(make_request(), model=provider.default_model))
    owned = CountingTransport.instances[0]

    asyncio.run(provider.aclose())

    assert owned.closed is True

    injected = FakeTransport([http_response(envelope(jev_payload()))])
    provider_with_injected = provider_with(injected)
    asyncio.run(provider_with_injected.aclose())
    assert injected.requests == [], "injected transports are never touched by aclose"


def test_app_lifespan_closes_the_owned_jev_transport(monkeypatch):
    from fastapi.testclient import TestClient

    import dungeon_director.cloudflare_jev as cloudflare_jev
    from dungeon_director.app import create_app

    CountingTransport.instances = []
    monkeypatch.setattr(cloudflare_jev, "HttpxJevTransport", CountingTransport)
    provider = CloudflareJevProvider(
        JevConfig(api_base_url="https://api.cloudflare.example", account_id="a", api_token="t")
    )
    registry = ProviderRegistry()
    registry.register(provider)
    settings = DirectorSettings(default_provider=provider.provider_id, timeout_seconds=1.0)

    with TestClient(create_app(settings=settings, registry=registry)) as client:
        response = client.post("/v1/generate", json=request_payload())
        assert response.status_code == 200
        owned = CountingTransport.instances[0]
        assert owned.closed is False

    assert owned.closed is True


def test_bool_usage_tokens_are_ignored_rather_than_coerced():
    payload = jev_payload()
    payload["usage"] = {"input_tokens": True, "output_tokens": "118"}
    transport = FakeTransport([http_response(envelope(payload))])

    result = generate(transport)

    assert result.usage is not None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


# --- request shape ------------------------------------------------------------


def test_request_matches_documented_cloudflare_contract():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport)

    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url == f"https://api.cloudflare.example/client/v4/accounts/{FAKE_ACCOUNT}/ai/run"
    assert request.headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request.headers["Content-Type"] == "application/json"

    body = request.json_body
    assert isinstance(body, dict)
    assert body["model"] == "typesafe/jev"
    assert set(body) == {"model", "input"}
    assert set(body["input"]) == {"state", "questions"}


def test_state_is_compact_json_safe_and_carries_the_frontier():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport)

    state = transport.requests[0].json_body["input"]["state"]
    json.dumps(state)  # must not raise: the whole state is JSON-safe
    assert state["depth"] == 3
    assert state["frontier"] == {"room_id": "r-003", "direction": "north", "since_turn": 145}
    assert state["player"]["hp_ratio"] == round(22 / 30, 3)
    assert state["options"]["forbidden_room_types"] == ["vault"]
    assert state["prompt_hint"]
    assert len(state["recent_rooms"]) <= 8
    assert len(state["recent_events"]) <= 8
    assert len(state["inventory"]) <= 24


def test_questions_use_only_documented_types_and_shapes():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport)

    questions = transport.requests[0].json_body["input"]["questions"]
    for question in questions.values():
        assert question["type"] in ("noul", "choice", "score")
        assert isinstance(question["instructions"], str) and question["instructions"]
        if question["type"] in ("choice", "score"):
            assert "criteria" in question
        if question["type"] == "choice":
            assert isinstance(question["criteria"], dict) and len(question["criteria"]) >= 2
        if question["type"] == "score":
            assert isinstance(question["criteria"], list) and len(question["criteria"]) >= 2
    assert questions["has_secret"]["type"] == "noul"
    assert questions["danger"]["type"] == "score"
    assert questions["room_type"]["type"] == "choice"


def test_question_set_covers_the_room_decision_and_all_tags():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport)

    questions = transport.requests[0].json_body["input"]["questions"]
    expected = {
        "room_type",
        "size",
        "danger",
        "enemy_density",
        "loot_density",
        "has_secret",
        "exit_count",
    }
    assert expected <= set(questions)
    assert {f"tag_{name}" for name in TAG_NAMES} <= set(questions)


def test_room_type_options_honour_forbidden_list_and_pacing_gates():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport, request_for_exit("north", options={"forbidden_room_types": ["vault"]}))

    criteria = transport.requests[0].json_body["input"]["questions"]["room_type"]["criteria"]
    assert "vault" not in criteria
    assert "entrance" not in criteria and "stairs_up" not in criteria

    early = FakeTransport([http_response(envelope(jev_payload()))])
    generate(early, request_for_exit("north", options={}, pacing={"rooms_on_depth": 2}))
    early_criteria = early.requests[0].json_body["input"]["questions"]["room_type"]["criteria"]
    assert "stairs_down" not in early_criteria


def test_hard_forbidden_list_wins_when_only_a_pacing_gated_type_remains():
    answers = jev_answers(
        room_type={
            "type": "choice",
            "choice": "stairs_down",
            "confidence": 1.0,
            "probabilities": {"stairs_down": 1.0},
        }
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])
    forbidden = [
        room_type.value
        for room_type in RoomType
        if room_type not in {RoomType.ENTRANCE, RoomType.STAIRS_DOWN, RoomType.STAIRS_UP}
    ]

    result = generate(
        transport,
        request_for_exit(
            "north",
            options={"forbidden_room_types": forbidden},
            pacing={"rooms_on_depth": 0},
        ),
    )

    criteria = transport.requests[0].json_body["input"]["questions"]["room_type"]["criteria"]
    assert set(criteria) == {"stairs_down"}
    assert result.payload.room_type is RoomType.STAIRS_DOWN


def test_request_body_matches_the_recorded_sanitized_fixture():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    generate(transport, base_url="https://api.cloudflare.com/client/v4")

    recorded = json.loads((FIXTURES / "sample_request.json").read_text(encoding="utf-8"))
    assert transport.requests[0].json_body == recorded


# --- success path -------------------------------------------------------------


def test_generate_composes_a_schema_valid_room_plan():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    result = generate(transport)

    room = result.payload
    assert isinstance(room, RoomPlan)
    request = make_request()
    assert room.depth == request.state.depth
    assert room.room_type is RoomType.CHAMBER
    assert room.danger == 3  # score 2.2 -> level 3, max_danger 3 allows it
    assert room.enemy_density == 0.2  # options.target_enemy_density honoured
    assert room.loot_density == 0.4  # options.target_loot_density honoured
    assert room.secret_probability == 0.61
    assert room.has_secret is True
    assert room.environmental_tags == [EnvironmentalTag.DARK]
    directions = [exit_.direction.value for exit_ in room.exits]
    assert directions == ["south", "north"]  # back-link first, then one extra
    assert room.exits[0].kind is ExitKind.DOOR
    assert room.exits[1].kind is ExitKind.SECRET  # has_secret marks the last extra
    assert room.room_id.startswith("jev-")
    assert room.description
    # The service re-validates anyway; assert it survives the canonical envelope.
    response = GenerationResponse.success_from_request(
        request,
        room=room,
        provider="cloudflare-jev",
        model="typesafe/jev",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        usage=result.usage,
        provider_metadata=result.provider_metadata,
    )
    assert response.success is True


def test_back_link_kind_is_stairs_for_vertical_frontiers():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    result = generate(transport, request=request_for_exit("down"))

    assert result.payload.exits[0].direction.value == "up"
    assert result.payload.exits[0].kind is ExitKind.STAIRS


def test_usage_and_metadata_are_preserved_and_bounded():
    transport = FakeTransport(
        [http_response(envelope(jev_payload()), headers={"cf-ray": "8f3a1b2c4d5e6f7g-lax"})]
    )

    result = generate(transport)

    assert result.usage is not None
    assert result.usage.input_tokens == 642
    assert result.usage.output_tokens == 118
    assert result.usage.estimated_cost_usd is None  # never fabricated
    metadata = result.provider_metadata
    assert metadata["jev_model"] == "jev-1.13.0"
    assert metadata["cf_ray"] == "8f3a1b2c4d5e6f7g-lax"
    assert metadata["upstream_http_status"] == 200
    assert isinstance(metadata["adapter_elapsed_ms"], float)
    assert metadata["exit_count"] == 1
    serialized = json.dumps(metadata)
    assert len(serialized.encode()) <= 8192 and len(metadata) <= 32
    assert FAKE_TOKEN not in serialized and FAKE_ACCOUNT not in serialized


def test_metadata_preserves_every_decision_signal():
    answers = jev_answers(
        size={
            "type": "choice",
            "choice": "large",
            "confidence": 0.44,
            "probabilities": {
                "small": 0.11,
                "medium": 0.2,
                "large": 0.66,
                "tiny": 0.01,
                "huge": 0.02,
            },
        },
        enemy_density={
            "type": "score",
            "score": 3.0,
            "confidence": 0.71,
            "legend": {"0": "e0", "1": "e1", "2": "e2", "3": "e3", "4": "e4"},
            "probabilities": {"0": 0.03, "1": 0.11, "2": 0.25, "3": 0.56, "4": 0.05},
        },
        loot_density={
            "type": "score",
            "score": 1.0,
            "confidence": 0.83,
            "legend": {"0": "l0", "1": "l1", "2": "l2", "3": "l3", "4": "l4"},
            "probabilities": {"0": 0.22, "1": 0.63, "2": 0.12, "3": 0.02, "4": 0.01},
        },
        has_secret={"type": "noul", "noul": 0.73},
        exit_count={
            "type": "choice",
            "choice": "2",
            "confidence": 0.51,
            "probabilities": {"0": 0.09, "1": 0.2, "2": 0.62, "3": 0.09},
        },
        tag_dark={"type": "noul", "noul": 0.91},
        tag_fungal={"type": "noul", "noul": 0.55},
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport)

    metadata = result.provider_metadata
    # room_type: choice + confidence + distribution
    assert metadata["room_type_confidence"] == 0.78
    assert metadata["room_type_probabilities"] == {
        "chamber": 0.56,
        "room": 0.21,
        "shrine": 0.09,
        "corridor": 0.05,
    }
    # size: confidence + distribution
    assert metadata["size_confidence"] == 0.44
    assert metadata["size_probabilities"]["large"] == 0.66
    # scores: raw score + confidence + distribution
    assert metadata["danger_score"] == 2.2
    assert metadata["danger_confidence"] == 0.84
    assert metadata["danger_probabilities"] == {"2": 0.67, "3": 0.15}
    assert metadata["enemy_density_score"] == 3.0
    assert metadata["enemy_density_confidence"] == 0.71
    assert metadata["enemy_density_probabilities"]["3"] == 0.56
    assert metadata["loot_density_score"] == 1.0
    assert metadata["loot_density_confidence"] == 0.83
    assert metadata["loot_density_probabilities"] == {
        "0": 0.22,
        "1": 0.63,
        "2": 0.12,
        "3": 0.02,
        "4": 0.01,
    }
    # nouls: the probability is the signal
    assert metadata["has_secret_probability"] == 0.73
    tag_probabilities = metadata["tag_probabilities"]
    assert len(tag_probabilities) == 10  # every tag question's noul, selected or not
    assert tag_probabilities["dark"] == 0.91
    assert tag_probabilities["fungal"] == 0.55
    assert tag_probabilities["noisy"] == TAG_BASE_PROBABILITY
    # exit_count: confidence + distribution
    assert metadata["exit_count_confidence"] == 0.51
    assert metadata["exit_count_probabilities"] == {"0": 0.09, "1": 0.2, "2": 0.62, "3": 0.09}
    # still inside the contract budget with every signal retained
    serialized = json.dumps(metadata)
    assert len(metadata) <= 32 and len(serialized.encode()) <= 8192


def test_bare_jev_payload_without_cloudflare_envelope_is_accepted():
    transport = FakeTransport([http_response(jev_payload())])

    result = generate(transport)

    assert isinstance(result.payload, RoomPlan)


def test_recorded_response_fixture_drives_a_valid_room():
    body = json.loads((FIXTURES / "sample_response.json").read_text(encoding="utf-8"))
    transport = FakeTransport([http_response(body)])

    result = generate(transport)

    room = result.payload
    assert isinstance(room, RoomPlan)
    assert room.room_type is RoomType.CHAMBER
    assert room.secret_probability == 0.61
    assert room.environmental_tags == [EnvironmentalTag.DARK]


# --- options, clamps and invariants -------------------------------------------


def test_max_danger_caps_the_scored_danger():
    answers = jev_answers(
        danger={"type": "score", "score": 4.0, "confidence": 0.9, "probabilities": {"4": 0.9}}
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport, request=request_for_exit("north", options={"max_danger": 2}))

    assert result.payload.danger == 2


@pytest.mark.parametrize(("score", "expected"), [(0.5, 2), (1.5, 3), (2.5, 4), (3.5, 5)])
def test_danger_scores_use_half_up_buckets(score, expected):
    answers = jev_answers(
        danger={
            "type": "score",
            "score": score,
            "confidence": 0.123456789,
            "probabilities": {str(int(score)): 1.0},
        }
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(
        transport,
        request=request_for_exit("north", options={"max_danger": 5}),
    )

    assert result.payload.danger == expected
    assert result.provider_metadata["danger_confidence"] == 0.123457


def test_boundary_scores_are_valid_without_clamping():
    answers = jev_answers(
        danger={
            "type": "score",
            "score": 4.0,
            "confidence": 0.9,
            "legend": {"4": "d5"},
            "probabilities": {"4": 1.0},
        },
        enemy_density={
            "type": "score",
            "score": 0.0,
            "confidence": 0.9,
            "legend": {"0": "e0"},
            "probabilities": {"0": 1.0},
        },
        loot_density={
            "type": "score",
            "score": 4.0,
            "confidence": 0.9,
            "legend": {"4": "l4"},
            "probabilities": {"4": 1.0},
        },
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport, request=request_for_exit("north", options={}))

    room = result.payload
    assert room.danger == 5
    assert room.enemy_density == 0.0
    assert room.loot_density == 1.0
    assert result.provider_metadata["danger_score"] == 4.0


def test_float_noise_at_rubric_boundaries_is_tolerated():
    answers = jev_answers(
        danger={
            "type": "score",
            "score": 4.000000001,
            "confidence": 0.9,
            "probabilities": {"4": 1.0000000001},
        },
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport, request=request_for_exit("north", options={}))

    assert result.payload.danger == 5
    assert result.provider_metadata["danger_probabilities"] == {"4": 1.0}


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "score", "score": 9.9, "confidence": 0.9},
        {"type": "score", "score": -5.0, "confidence": 0.9},
        {"type": "score", "score": float("nan"), "confidence": 0.9},
        {"type": "score", "score": float("inf"), "confidence": 0.9},
        {"type": "score", "score": True, "confidence": 0.9},
        {"type": "score", "score": "2", "confidence": 0.9},
        {"type": "score", "score": 2.2},
        {"type": "score", "score": 2.2, "confidence": 1.5},
        {"type": "score", "score": 2.2, "confidence": True},
        {"type": "score", "score": 2.2, "confidence": 0.9, "probabilities": {"9": 1.0}},
        {"type": "score", "score": 2.2, "confidence": 0.9, "probabilities": {"2": 1.5}},
    ],
    ids=[
        "score-above-rubric",
        "score-below-zero",
        "score-nan",
        "score-inf",
        "score-bool",
        "score-string",
        "missing-confidence",
        "confidence-above-one",
        "confidence-bool",
        "probability-unknown-level",
        "probability-out-of-range",
    ],
)
def test_hostile_score_answers_are_schema_violations_never_clamped(answer):
    answers = jev_answers(danger=answer)
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport, request=request_for_exit("north", options={}))

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "noul", "noul": 1.2},
        {"type": "noul", "noul": -0.1},
        {"type": "noul", "noul": float("nan")},
        {"type": "noul", "noul": float("-inf")},
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": "0.9"},
        {"type": "noul"},
    ],
    ids=[
        "noul-above-one",
        "noul-below-zero",
        "noul-nan",
        "noul-neg-inf",
        "noul-bool",
        "noul-string",
        "noul-missing",
    ],
)
def test_hostile_noul_answers_are_schema_violations(answer):
    answers = jev_answers(has_secret=answer)
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


@pytest.mark.parametrize(
    "answer",
    [
        {"type": "choice", "choice": "7", "confidence": 0.9, "probabilities": {"1": 1.0}},
        {"type": "choice", "choice": 1, "confidence": 0.9, "probabilities": {"1": 1.0}},
        {"type": "choice", "choice": "1", "confidence": 0.9, "probabilities": {"9": 1.0}},
        {"type": "choice", "choice": "1", "confidence": 0.9, "probabilities": {"1": float("nan")}},
        {"type": "choice", "choice": "1", "probabilities": {"1": 1.0}},
    ],
    ids=[
        "exit-count-out-of-offer",
        "choice-not-a-string",
        "probability-unknown-option",
        "probability-nan",
        "missing-confidence",
    ],
)
def test_hostile_choice_answers_are_schema_violations(answer):
    answers = jev_answers(exit_count=answer)
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_densities_scale_from_the_rubric_when_no_target_is_set():
    answers = jev_answers(
        enemy_density={
            "type": "score",
            "score": 2.0,
            "confidence": 0.9,
            "legend": {"2": "steady"},
            "probabilities": {"2": 0.9},
        },
        loot_density={
            "type": "score",
            "score": 3.0,
            "confidence": 0.9,
            "legend": {"3": "rich"},
            "probabilities": {"3": 0.9},
        },
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport, request=request_for_exit("north", options={}))

    assert result.payload.enemy_density == 0.5
    assert result.payload.loot_density == 0.75


def test_disallowed_secrets_remove_the_secret_entirely():
    transport = FakeTransport([http_response(envelope(jev_payload()))])

    result = generate(
        transport, request=request_for_exit("north", options={"allow_secrets": False})
    )

    room = result.payload
    assert room.has_secret is False
    assert room.secret_probability == 0.0
    assert all(exit_.kind is not ExitKind.SECRET for exit_ in room.exits)
    assert result.provider_metadata["has_secret_probability"] == 0.61
    assert result.provider_metadata["secret_allowed"] is False


def test_disallowed_secret_answer_is_still_validated():
    answers = jev_answers(has_secret={"type": "noul", "noul": float("nan")})
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(
            transport,
            request=request_for_exit("north", options={"allow_secrets": False}),
        )

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_unknown_model_is_rejected_locally_without_a_network_call():
    transport = FakeTransport()
    provider = provider_with(transport)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model="typesafe/does-not-exist"))

    assert info.value.code is ErrorKind.PROVIDER_ERROR
    assert transport.requests == []


def test_noul_threshold_keeps_the_room_plan_secret_invariant():
    answers = jev_answers(has_secret={"type": "noul", "noul": 0.5})
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport)

    room = result.payload
    assert room.has_secret is True
    assert room.secret_probability >= 0.5  # invariant: has_secret => probability > 0


def test_exit_directions_stay_unique_and_within_the_free_cardinals():
    answers = jev_answers(
        exit_count={
            "type": "choice",
            "choice": "3",
            "confidence": 0.7,
            "probabilities": {"3": 0.7},
        }
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport, request=request_for_exit("east", options=None))

    directions = [exit_.direction.value for exit_ in result.payload.exits]
    assert directions[0] == "west"  # back-link opposite the east frontier
    assert len(directions) == len(set(directions)) == 4
    assert set(directions[1:]) <= {"north", "south", "east"}


def test_tag_contradictions_are_resolved_and_the_cap_is_enforced():
    probabilities = {name: 0.9 for name in TAG_NAMES}
    probabilities["icy"] = 0.95
    probabilities["hot"] = 0.85
    answers = jev_answers(
        **{f"tag_{name}": {"type": "noul", "noul": value} for name, value in probabilities.items()}
    )
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    result = generate(transport)

    tags = result.payload.environmental_tags
    assert len(tags) == 8  # 10 selected, capped at the contract maximum
    assert not ({EnvironmentalTag.ICY, EnvironmentalTag.HOT} <= set(tags))
    assert EnvironmentalTag.ICY in tags  # the stronger signal survives


def test_room_id_is_deterministic_per_request_and_model():
    transport_a = FakeTransport([http_response(envelope(jev_payload()))])
    transport_b = FakeTransport([http_response(envelope(jev_payload()))])

    result_a = generate(transport_a)
    result_b = generate(transport_b)

    assert result_a.payload.room_id == result_b.payload.room_id
    assert result_a.payload.room_id.startswith("jev-")


# --- classified failures ------------------------------------------------------


def test_authentication_failure_maps_to_provider_error():
    transport = FakeTransport([http_response({"errors": [{"code": 1000}]}, status=401)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.PROVIDER_ERROR


def test_rate_limit_maps_to_rate_limited():
    transport = FakeTransport([http_response({}, status=429)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.RATE_LIMITED


def test_upstream_5xx_maps_to_provider_error():
    transport = FakeTransport([http_response({}, status=503)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.PROVIDER_ERROR


def test_transport_timeout_maps_to_provider_timeout():
    transport = FakeTransport(error=httpx.ReadTimeout("connection timed out"))

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.PROVIDER_TIMEOUT


def test_transport_network_failure_maps_to_provider_error():
    transport = FakeTransport(error=httpx.ConnectError("dns failure"))

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.PROVIDER_ERROR


def test_malformed_json_body_maps_to_invalid_json():
    transport = FakeTransport([http_response(b"<html>gateway error</html>", status=200)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.INVALID_JSON


def test_failure_envelope_maps_to_provider_error_without_body_text():
    body = {
        "success": False,
        "errors": [{"code": 9119, "message": "secret internal detail"}],
        "messages": [],
    }
    transport = FakeTransport([http_response(body)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    error = provider_error_from(info)
    assert error.code is ErrorKind.PROVIDER_ERROR
    assert "secret internal detail" not in error.message


def test_payload_without_answers_maps_to_schema_violation():
    transport = FakeTransport([http_response(envelope({"model": "jev-1.13.0"}))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_choice_outside_the_offered_options_is_rejected():
    answers = jev_answers(room_type={"type": "choice", "choice": "castle", "confidence": 0.9})
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_forbidden_room_type_choice_is_rejected_even_if_returned():
    answers = jev_answers(room_type={"type": "choice", "choice": "vault", "confidence": 0.9})
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(
            transport,
            request=request_for_exit("north", options={"forbidden_room_types": ["vault"]}),
        )

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_missing_answer_is_rejected():
    answers = jev_answers()
    del answers["size"]
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_wrong_answer_type_is_rejected():
    answers = jev_answers(danger={"type": "choice", "choice": "2", "confidence": 0.9})
    transport = FakeTransport([http_response(envelope(jev_payload(answers)))])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert provider_error_from(info).code is ErrorKind.SCHEMA_VIOLATION


def test_unknown_model_is_rejected_before_any_call():
    transport = FakeTransport([])
    provider = provider_with(transport)

    with pytest.raises(ProviderError) as info:
        asyncio.run(provider.generate(make_request(), model="typesafe/other"))

    assert provider_error_from(info).code is ErrorKind.PROVIDER_ERROR
    assert transport.requests == []


@pytest.mark.parametrize(
    "case",
    [
        http_response({"errors": []}, status=401),
        http_response({}, status=429),
        http_response({}, status=503),
        http_response(b"<html/>"),
    ],
    ids=["auth", "rate-limit", "upstream", "html-body"],
)
def test_error_messages_never_carry_credentials_or_bodies(case):
    transport = FakeTransport([case])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    message = provider_error_from(info).message
    assert FAKE_TOKEN not in message and FAKE_ACCOUNT not in message


def test_no_hidden_retries_exactly_one_call_per_generate():
    transport = FakeTransport([http_response({}, status=500)])

    with pytest.raises(ProviderError):
        generate(transport)

    assert len(transport.requests) == 1


def test_cancellation_propagates_out_of_generate():
    transport = FakeTransport(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        generate(transport)


# --- registry and service integration -----------------------------------------


def test_registry_accepts_the_provider_and_its_model_id():
    registry = ProviderRegistry()
    registry.register(provider_with(FakeTransport()))

    selection = registry.select("cloudflare-jev", None)

    assert selection.model == "typesafe/jev"
    assert selection.provider.provider_id == "cloudflare-jev"


def test_default_registry_keeps_rules_default_and_flags_jev_unavailable(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    from dungeon_director.registry import default_registry

    described = {d.id: d for d in default_registry().describe()}

    assert set(described) == {"rules-baseline", "cloudflare-jev", "groq", "cerebras"}
    assert described["rules-baseline"].available is True
    assert described["cloudflare-jev"].available is False
    assert described["cloudflare-jev"].default_model == "typesafe/jev"


def test_selecting_unconfigured_jev_reports_unavailable_through_the_service(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    from dungeon_director.registry import default_registry

    service = DirectorService(default_registry(), DirectorSettings())
    outcome = asyncio.run(service.generate(make_request(), provider="cloudflare-jev"))

    assert outcome.status_code == 503
    assert outcome.response.success is False
    assert outcome.response.metadata.error is not None
    assert outcome.response.metadata.provider == "cloudflare-jev"
    assert outcome.response.metadata.provider_metadata == {
        "selection_error": "provider_unavailable"
    }


def test_offline_default_config_survives_jev_registration(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    from fastapi.testclient import TestClient

    from dungeon_director.app import create_app

    client = TestClient(create_app())
    config = client.get("/v1/config").json()

    assert config["default_provider"] == "rules-baseline"
    providers = {p["id"]: p for p in config["providers"]}
    assert providers["cloudflare-jev"]["available"] is False
    assert providers["rules-baseline"]["available"] is True

    response = client.post("/v1/generate?provider=cloudflare-jev", json=request_payload())

    assert response.status_code == 503
    assert response.json()["metadata"]["provider"] == "cloudflare-jev"
    assert FAKE_TOKEN not in response.text
