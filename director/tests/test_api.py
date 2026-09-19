"""HTTP boundary: /v1/generate, /v1/config, /health."""

from __future__ import annotations

import json

import pytest
from fakes import (
    BrokenAvailabilityProvider,
    ClassifiedFailureProvider,
    FakeProvider,
    MalformedProvider,
    RaisingProvider,
    RawResultProvider,
    SlowProvider,
    SuppressingProvider,
    request_payload,
    valid_room_dict,
)
from fastapi.testclient import TestClient

from dungeon_director import groq
from dungeon_director.app import create_app
from dungeon_director.contracts import (
    CONTRACT_VERSION,
    UNKNOWN_REQUEST_ID,
    UNKNOWN_RUN_ID,
    ErrorKind,
    GenerationRequest,
    GenerationResponse,
    RoomPlan,
)
from dungeon_director.errors import ProviderError
from dungeon_director.groq import GroqTransportResponse
from dungeon_director.providers import DungeonDirectorProvider
from dungeon_director.registry import ProviderRegistry, default_registry
from dungeon_director.settings import DirectorSettings


def make_client(
    *providers: DungeonDirectorProvider,
    default: str | None = None,
    default_model: str | None = None,
    timeout: float = 1.0,
) -> TestClient:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    settings = DirectorSettings(
        default_provider=default or providers[0].provider_id,
        default_model=default_model,
        timeout_seconds=timeout,
    )
    return TestClient(create_app(settings=settings, registry=registry))


def rules_client() -> TestClient:
    return TestClient(create_app(settings=DirectorSettings(), registry=default_registry()))


def parse(response) -> GenerationResponse:
    """Every /v1/generate body, whatever the status, must be a canonical envelope."""
    return GenerationResponse.model_validate(response.json())


# --- health and config -------------------------------------------------------


def test_health_is_unchanged():
    response = rules_client().get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "dungeon-director"}


def test_config_exposes_default_and_provider_identifiers_only():
    client = make_client(
        FakeProvider("alpha", models=("a1", "a2")),
        FakeProvider("beta", available=False),
        default="alpha",
        default_model="a2",
    )

    body = client.get("/v1/config").json()

    assert body == {
        "default_provider": "alpha",
        "default_model": "a2",
        "providers": [
            {"id": "alpha", "available": True, "default_model": "a1", "models": ["a1", "a2"]},
            {
                "id": "beta",
                "available": False,
                "default_model": "fake-model",
                "models": ["fake-model"],
            },
        ],
    }


def test_config_default_model_falls_back_to_the_providers_own_default():
    body = rules_client().get("/v1/config").json()

    assert (body["default_provider"], body["default_model"]) == ("rules-baseline", "builtin-v1")


def test_config_and_generate_never_expose_credentials(monkeypatch):
    secret = "sk-live-9f8e7d6c5b4a"
    for name in (
        "GROQ_API_KEY",
        "CEREBRAS_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    ):
        monkeypatch.setenv(name, secret)
    client = TestClient(create_app())

    class EchoingGroqTransport:
        """Offline stand-in for the real Groq client: rejects the key and echoes it."""

        async def send(self, request):
            body = json.dumps({"error": {"message": f"invalid key {secret}"}}).encode()
            return GroqTransportResponse(status_code=401, headers={}, body=body)

    monkeypatch.setattr(groq, "HttpxGroqTransport", EchoingGroqTransport)

    groq_response = client.post("/v1/generate?provider=groq", json=request_payload())
    assert groq_response.status_code == 502, "the request must reach the (stubbed) Groq adapter"
    assert groq_response.json()["metadata"]["error"]["code"] == "provider_error"

    bodies = [
        client.get("/health").text,
        client.get("/v1/config").text,
        client.get("/openapi.json").text,
        client.post("/v1/generate", json=request_payload()).text,
        groq_response.text,
        client.post("/v1/generate", content=b"{not json").text,
    ]

    assert all(secret not in body for body in bodies)


def test_config_is_read_only_and_health_rejects_post():
    client = rules_client()

    assert client.post("/v1/config").status_code == 405
    assert client.post("/health").status_code == 405
    assert client.get("/v1/generate").status_code == 405


# --- generate: success and selection -----------------------------------------


def test_generate_returns_a_valid_rules_plan_by_default():
    response = rules_client().post("/v1/generate", json=request_payload())

    assert response.status_code == 200
    body = parse(response)
    request = GenerationRequest.model_validate(request_payload())
    assert body.success is True
    assert body.request_id == request.request_id
    assert body.metadata.provider == "rules-baseline"
    assert body.room is not None and body.room.depth == request.state.depth
    assert body.metadata.error is None


def test_generate_is_deterministic_over_http():
    client = rules_client()

    first = client.post("/v1/generate", json=request_payload()).json()["room"]
    second = client.post("/v1/generate", json=request_payload(request_id="req-other")).json()[
        "room"
    ]

    assert first == second


def test_query_selects_provider_and_model():
    alpha, beta = FakeProvider("alpha"), FakeProvider("beta", models=("m1", "m2"))
    client = make_client(alpha, beta)

    response = client.post("/v1/generate?provider=beta&model=m2", json=request_payload())

    body = parse(response)
    assert response.status_code == 200
    assert (body.metadata.provider, body.metadata.model) == ("beta", "m2")
    assert (alpha.calls, beta.calls) == (0, 1)


def test_request_body_contract_is_unchanged_by_selection():
    """Selection is transport-level: the shared GenerationRequest has no provider field."""
    payload = request_payload(provider="beta")

    response = make_client(FakeProvider("alpha")).post("/v1/generate", json=payload)

    assert response.status_code == 422
    assert parse(response).metadata.error.code is ErrorKind.SCHEMA_VIOLATION


def test_unknown_provider_is_404_with_a_clear_canonical_error():
    response = make_client(FakeProvider("alpha")).post(
        "/v1/generate?provider=gpt-nine", json=request_payload()
    )

    body = parse(response)
    assert response.status_code == 404
    assert body.success is False and body.room is None
    assert "gpt-nine" in body.metadata.error.message
    assert "alpha" in body.metadata.error.message


def test_unavailable_provider_is_503_and_is_not_called():
    down = FakeProvider("beta", available=False)

    response = make_client(FakeProvider("alpha"), down).post(
        "/v1/generate?provider=beta", json=request_payload()
    )

    assert response.status_code == 503
    assert "unavailable" in parse(response).metadata.error.message
    assert down.calls == 0


def test_unknown_model_is_404():
    response = make_client(FakeProvider("alpha")).post(
        "/v1/generate?provider=alpha&model=nope", json=request_payload()
    )

    assert response.status_code == 404
    assert "nope" in parse(response).metadata.error.message


def test_overlong_selection_parameter_is_rejected_as_a_canonical_422():
    response = rules_client().post("/v1/generate?provider=" + "x" * 300, json=request_payload())

    assert response.status_code == 422
    assert parse(response).success is False


# --- generate: provider failures ---------------------------------------------


def test_timeout_is_504_and_service_keeps_serving():
    slow, ok = SlowProvider(), FakeProvider("ok")
    client = make_client(slow, ok, default="ok", timeout=0.05)

    timed_out = client.post("/v1/generate?provider=slow", json=request_payload())
    after = client.post("/v1/generate", json=request_payload())

    assert timed_out.status_code == 504
    assert parse(timed_out).metadata.error.code is ErrorKind.PROVIDER_TIMEOUT
    assert slow.calls == 1 and slow.cancelled is True
    assert after.status_code == 200


def test_provider_exception_is_502_and_never_crashes_the_service():
    client = make_client(FakeProvider("ok"), RaisingProvider(RuntimeError("kaboom"), "bad"))

    failed = client.post("/v1/generate?provider=bad", json=request_payload())
    after = client.post("/v1/generate", json=request_payload())

    assert failed.status_code == 502
    assert parse(failed).metadata.error.code is ErrorKind.PROVIDER_ERROR
    assert after.status_code == 200


def test_classified_provider_failure_keeps_its_code():
    provider = ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "slow down")

    response = make_client(provider).post("/v1/generate", json=request_payload())

    assert response.status_code == 429
    assert parse(response).metadata.error.code is ErrorKind.RATE_LIMITED


def test_malformed_provider_output_is_a_502_schema_violation():
    bad = MalformedProvider({"room_id": "r", "depth": 3, "room_type": "nonsense", "size": "small"})

    response = make_client(bad).post("/v1/generate", json=request_payload())

    assert response.status_code == 502
    assert parse(response).metadata.error.code is ErrorKind.SCHEMA_VIOLATION


def test_provider_output_for_the_wrong_depth_is_rejected():
    room = valid_room_dict(GenerationRequest.model_validate(request_payload()))
    room["depth"] = 99

    response = make_client(MalformedProvider(room)).post("/v1/generate", json=request_payload())

    assert response.status_code == 502
    assert parse(response).metadata.error.code is ErrorKind.SCHEMA_VIOLATION


# --- generate: invalid requests ----------------------------------------------


def test_invalid_request_body_is_a_canonical_422_that_echoes_ids():
    payload = request_payload()
    payload["state"]["player"]["hp"] = 999  # exceeds max_hp

    response = rules_client().post("/v1/generate", json=payload)

    body = parse(response)
    assert response.status_code == 422
    assert body.metadata.error.code is ErrorKind.SCHEMA_VIOLATION
    assert (body.request_id, body.run_id) == (payload["request_id"], payload["run_id"])
    assert body.contract_version == CONTRACT_VERSION
    assert "hp" in body.metadata.error.message


def test_target_exit_off_the_frontier_is_rejected():
    payload = request_payload(target_exit={"room_id": "r-999", "direction": "east"})

    response = rules_client().post("/v1/generate", json=payload)

    assert response.status_code == 422
    assert "unresolved_exits" in parse(response).metadata.error.message


def test_unsupported_contract_version_has_its_own_error_code():
    response = rules_client().post("/v1/generate", json=request_payload(contract_version="2.0.0"))

    assert response.status_code == 422
    assert parse(response).metadata.error.code is ErrorKind.UNSUPPORTED_CONTRACT_VERSION


def test_unparseable_json_body_is_invalid_json():
    response = rules_client().post(
        "/v1/generate", content=b"{not json", headers={"content-type": "application/json"}
    )

    body = parse(response)
    assert response.status_code == 422
    assert body.metadata.error.code is ErrorKind.INVALID_JSON
    assert (body.request_id, body.run_id) == (UNKNOWN_REQUEST_ID, UNKNOWN_RUN_ID)


@pytest.mark.parametrize("payload", [[], "text", 7, None])
def test_non_object_json_body_never_breaks_the_error_path(payload):
    response = rules_client().post("/v1/generate", json=payload)

    assert response.status_code == 422
    assert parse(response).metadata.error.code is ErrorKind.SCHEMA_VIOLATION


def test_invalid_ids_in_a_bad_request_fall_back_to_sentinels():
    response = rules_client().post(
        "/v1/generate", json={"request_id": "bad id!", "run_id": 5, "state": {}}
    )

    body = parse(response)
    assert (body.request_id, body.run_id) == (UNKNOWN_REQUEST_ID, UNKNOWN_RUN_ID)


def test_validation_message_does_not_echo_submitted_values():
    payload = request_payload(prompt_hint="x" * 501)
    payload["state"]["player"]["conditions"] = ["SECRET-CONDITION-VALUE"] * 40

    message = parse(rules_client().post("/v1/generate", json=payload)).metadata.error.message

    assert "SECRET-CONDITION-VALUE" not in message


# --- factory isolation -------------------------------------------------------


def test_apps_do_not_share_registries_or_state():
    first = make_client(FakeProvider("only-first"))
    second = make_client(FakeProvider("only-second"))

    assert [p["id"] for p in first.get("/v1/config").json()["providers"]] == ["only-first"]
    assert [p["id"] for p in second.get("/v1/config").json()["providers"]] == ["only-second"]
    assert (
        second.post("/v1/generate?provider=only-first", json=request_payload()).status_code == 404
    )


def test_bad_default_provider_fails_app_creation():
    from dungeon_director.errors import DirectorConfigError

    with pytest.raises(DirectorConfigError):
        create_app(
            settings=DirectorSettings(default_provider="missing"), registry=default_registry()
        )


def test_generate_response_room_round_trips_through_the_contract():
    body = rules_client().post("/v1/generate", json=request_payload()).json()

    assert RoomPlan.model_validate(body["room"]).depth == 3
    assert json.dumps(body)  # plain JSON, no custom types


def test_crashing_availability_check_keeps_config_usable_and_generate_a_canonical_503():
    client = make_client(FakeProvider("ok"), BrokenAvailabilityProvider(), default="ok")

    config = client.get("/v1/config")
    generated = client.post("/v1/generate?provider=broken", json=request_payload())
    default = client.post("/v1/generate", json=request_payload())

    assert config.status_code == 200
    assert {p["id"]: p["available"] for p in config.json()["providers"]} == {
        "ok": True,
        "broken": False,
    }
    assert generated.status_code == 503
    assert parse(generated).metadata.provider_metadata == {
        "selection_error": "provider_unavailable"
    }
    assert "availability probe crashed" not in generated.text
    assert default.status_code == 200


def test_classified_provider_error_text_and_excerpt_never_reach_the_client():
    secret = "sk-super-secret"
    leaky = RaisingProvider(
        ProviderError(
            ErrorKind.PROVIDER_ERROR,
            f"upstream key {secret} failed",
            raw_excerpt=f"Authorization: Bearer {secret}",
        ),
        "leaky",
    )

    response = make_client(leaky).post("/v1/generate", json=request_payload())

    assert response.status_code == 502
    assert secret not in response.text and "Bearer" not in response.text
    assert parse(response).metadata.error.code is ErrorKind.PROVIDER_ERROR
    assert parse(response).metadata.error.raw_excerpt is None


@pytest.mark.parametrize(
    "query", ["provider=", "provider=%20%20", "provider=%09", "model=&provider="]
)
def test_blank_selectors_behave_as_omitted(query):
    default, other = FakeProvider("alpha"), FakeProvider("beta")
    client = make_client(default, other, default="alpha")

    response = client.post(f"/v1/generate?{query}", json=request_payload())

    assert response.status_code == 200
    assert parse(response).metadata.provider == "alpha"
    assert (default.calls, other.calls) == (1, 0)


def test_blank_model_uses_the_default_model_and_padded_selectors_are_trimmed():
    provider = FakeProvider("alpha", models=("m1", "m2"))
    client = make_client(provider, default="alpha", default_model="m2")

    blank = client.post("/v1/generate?model=%20", json=request_payload())
    padded = client.post("/v1/generate?provider=%20alpha%20&model=%20m1%20", json=request_payload())

    assert parse(blank).metadata.model == "m2"
    assert (padded.status_code, parse(padded).metadata.model) == (200, "m1")


def test_malformed_provider_result_object_is_a_502_over_http():
    response = make_client(RawResultProvider({"not": "a ProviderResult"})).post(
        "/v1/generate", json=request_payload()
    )

    assert response.status_code == 502
    assert parse(response).metadata.error.code is ErrorKind.SCHEMA_VIOLATION


def test_late_return_after_the_deadline_is_a_504_over_http():
    response = make_client(SuppressingProvider(), timeout=0.05).post(
        "/v1/generate", json=request_payload()
    )

    assert response.status_code == 504
    assert parse(response).metadata.error.code is ErrorKind.PROVIDER_TIMEOUT
