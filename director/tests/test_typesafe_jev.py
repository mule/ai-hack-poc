"""Direct TypeSafe Jev adapter tests; every upstream response is local and canned."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fakes import make_request

from dungeon_director.cloudflare_jev import JevTransportRequest, JevTransportResponse
from dungeon_director.contracts import RoomPlan, RoomType
from dungeon_director.errors import DirectorConfigError, ErrorKind, ProviderError
from dungeon_director.registry import default_registry
from dungeon_director.typesafe_jev import (
    DEFAULT_TYPESAFE_JEV_API_URL,
    DEFAULT_TYPESAFE_JEV_MODEL,
    TypeSafeJevConfig,
    TypeSafeJevProvider,
)

FAKE_KEY = "ts-secret-key-do-not-print"


class FakeTransport:
    def __init__(self, responses: list[Any] | None = None, error: Exception | None = None) -> None:
        self.requests: list[JevTransportRequest] = []
        self._responses = list(responses or [])
        self._error = error

    async def send(self, request: JevTransportRequest) -> JevTransportResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def response(body: Any, status: int = 200) -> JevTransportResponse:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return JevTransportResponse(status_code=status, headers={}, body=content)


def answers() -> dict[str, Any]:
    result: dict[str, Any] = {
        "room_type": {
            "type": "choice",
            "choice": "chamber",
            "confidence": 0.8,
            "probabilities": {"chamber": 0.8, "room": 0.2},
        },
        "size": {
            "type": "choice",
            "choice": "medium",
            "confidence": 0.9,
            "probabilities": {"medium": 0.9, "small": 0.1},
        },
        "danger": {
            "type": "score",
            "score": 2.0,
            "confidence": 0.8,
            "legend": {str(i): f"danger-{i}" for i in range(5)},
            "probabilities": {"2": 1.0},
        },
        "enemy_density": {
            "type": "score",
            "score": 1.0,
            "confidence": 0.8,
            "legend": {str(i): f"enemy-{i}" for i in range(5)},
            "probabilities": {"1": 1.0},
        },
        "loot_density": {
            "type": "score",
            "score": 3.0,
            "confidence": 0.8,
            "legend": {str(i): f"loot-{i}" for i in range(5)},
            "probabilities": {"3": 1.0},
        },
        "has_secret": {"type": "noul", "noul": 0.2},
        "exit_count": {
            "type": "choice",
            "choice": "1",
            "confidence": 0.8,
            "probabilities": {"1": 1.0},
        },
        "atmosphere": {
            "type": "choice",
            "choice": "none",
            "confidence": 0.8,
            "probabilities": {"none": 0.8, "dark": 0.2},
        },
    }
    return result


def payload() -> dict[str, Any]:
    return {
        "model": "jev-1.13.0",
        "answers": answers(),
        "usage": {"input_tokens": 500, "output_tokens": 80},
    }


def provider_with(transport: FakeTransport, **kwargs: Any) -> TypeSafeJevProvider:
    return TypeSafeJevProvider(
        TypeSafeJevConfig(api_key=FAKE_KEY, **kwargs),
        transport,
    )


def generate(transport: FakeTransport) -> Any:
    provider = provider_with(transport)
    return asyncio.run(provider.generate(make_request(), model=provider.default_model))


def test_config_defaults_env_and_availability():
    empty = TypeSafeJevConfig.from_env({})
    configured = TypeSafeJevConfig.from_env(
        {
            "TYPESAFE_API_KEY": FAKE_KEY,
            "TYPESAFE_JEV_MODEL": "jev-1.13.0",
            "TYPESAFE_JEV_API_URL": "https://proxy.example/v1/systemone/",
        }
    )

    assert empty.model == DEFAULT_TYPESAFE_JEV_MODEL == "jev-latest"
    assert empty.api_url == DEFAULT_TYPESAFE_JEV_API_URL
    assert TypeSafeJevProvider(empty).availability.available is False
    assert configured.model == "jev-1.13.0"
    assert configured.api_url == "https://proxy.example/v1/systemone"
    assert TypeSafeJevProvider(configured).availability.available is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", "key with spaces"),
        ("model", "bad model id"),
        ("api_url", "http://remote.example/v1/systemone"),
        ("api_url", "https://api.typesafe.ai/v1/systemone?key=secret"),
    ],
)
def test_config_rejects_header_and_url_injection(field: str, value: str):
    values = {"api_key": FAKE_KEY, "model": "jev-latest", "api_url": DEFAULT_TYPESAFE_JEV_API_URL}
    values[field] = value

    with pytest.raises(DirectorConfigError) as info:
        TypeSafeJevConfig(**values)

    assert value not in str(info.value)


def test_credentials_do_not_appear_in_repr():
    config = TypeSafeJevConfig(api_key=FAKE_KEY)
    provider = TypeSafeJevProvider(config)

    assert FAKE_KEY not in repr(config)
    assert FAKE_KEY not in repr(provider)


def test_request_matches_the_direct_typesafe_contract():
    transport = FakeTransport([response(payload())])

    generate(transport)

    (request,) = transport.requests
    assert request.method == "POST"
    assert request.url == "https://api.typesafe.ai/v1/systemone"
    assert request.headers == {
        "Authorization": f"Bearer {FAKE_KEY}",
        "Content-Type": "application/json",
    }
    assert set(request.json_body) == {"state", "model", "questions"}
    assert request.json_body["model"] == "jev-latest"
    assert request.json_body["state"]["frontier"]["direction"] == "north"
    assert request.json_body["questions"]["room_type"]["type"] == "choice"


def test_direct_response_composes_a_canonical_room_and_preserves_usage():
    result = generate(FakeTransport([response(payload())]))

    assert isinstance(result.payload, RoomPlan)
    assert result.payload.room_type is RoomType.CHAMBER
    assert result.payload.danger == 3
    assert result.payload.enemy_density == 0.2
    assert result.payload.loot_density == 0.4
    assert result.provider_metadata["enemy_density_score"] == 1.0
    assert result.provider_metadata["loot_density_score"] == 3.0
    assert result.usage.input_tokens == 500
    assert result.provider_metadata["jev_model"] == "jev-1.13.0"
    assert result.provider_metadata["upstream_http_status"] == 200
    assert result.provider_metadata["room_type_confidence"] == 0.8


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ErrorKind.PROVIDER_ERROR),
        (422, ErrorKind.PROVIDER_ERROR),
        (429, ErrorKind.RATE_LIMITED),
        (529, ErrorKind.PROVIDER_ERROR),
        (500, ErrorKind.PROVIDER_ERROR),
    ],
)
def test_documented_http_failures_are_classified_without_body_text(status: int, kind: ErrorKind):
    transport = FakeTransport([response({"detail": FAKE_KEY}, status=status)])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert info.value.code is kind
    assert FAKE_KEY not in info.value.message
    assert len(transport.requests) == 1


def test_invalid_json_is_classified_without_retries():
    transport = FakeTransport([response(b"not-json")])

    with pytest.raises(ProviderError) as info:
        generate(transport)

    assert info.value.code is ErrorKind.INVALID_JSON
    assert len(transport.requests) == 1


def test_transport_timeout_and_cancellation_have_distinct_behavior():
    timeout = FakeTransport(error=httpx.ReadTimeout("secret upstream text"))
    with pytest.raises(ProviderError) as info:
        generate(timeout)
    assert info.value.code is ErrorKind.PROVIDER_TIMEOUT
    assert "secret upstream text" not in info.value.message

    cancelled = FakeTransport(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        generate(cancelled)


def test_unknown_model_and_missing_key_fail_before_transport():
    transport = FakeTransport()
    provider = provider_with(transport)
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate(make_request(), model="jev-other"))

    missing = TypeSafeJevProvider(TypeSafeJevConfig(), transport)
    with pytest.raises(ProviderError):
        asyncio.run(missing.generate(make_request(), model=missing.default_model))

    assert transport.requests == []


def test_default_registry_exposes_direct_typesafe_when_key_is_configured(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)

    registry = default_registry()
    described = {item.id: item for item in registry.describe()}

    assert described["typesafe-jev"].available is True
    assert described["typesafe-jev"].default_model == "jev-latest"
    assert registry.select("typesafe-jev", None).model == "jev-latest"
    assert FAKE_KEY not in json.dumps([item.model_dump() for item in described.values()])


def test_invalid_optional_typesafe_config_keeps_offline_provider_available(monkeypatch, caplog):
    monkeypatch.setenv("TYPESAFE_API_KEY", FAKE_KEY)
    monkeypatch.setenv("TYPESAFE_JEV_API_URL", "http://remote.example/v1/systemone")

    registry = default_registry()
    described = {item.id: item for item in registry.describe()}

    assert described["rules-baseline"].available is True
    assert described["typesafe-jev"].available is False
    assert FAKE_KEY not in caplog.text
