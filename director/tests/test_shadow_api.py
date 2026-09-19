"""Shadow evaluation through the HTTP boundary and the app lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time

from fakes import FakeProvider, RaisingProvider, request_payload
from fastapi.testclient import TestClient
from shadow_fakes import CANARY, GatedProvider

from dungeon_director.app import COMPARISON_ID_HEADER, create_app
from dungeon_director.registry import ProviderRegistry, default_registry
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget

VOLATILE = ("started_at", "completed_at", "latency_ms")


def build(active, shadows=(), *, drain_seconds=0.05, **shadow_kwargs):
    registry = ProviderRegistry()
    for provider in (active, *shadows):
        registry.register(provider)
    settings = DirectorSettings(
        default_provider=active.provider_id,
        timeout_seconds=5,
        shadow=ShadowSettings(
            targets=tuple(ShadowTarget(p.provider_id) for p in shadows),
            drain_seconds=drain_seconds,
            **shadow_kwargs,
        ),
    )
    return create_app(settings, registry)


def stable_body(response):
    body = response.json()
    for name in VOLATILE:
        body["metadata"].pop(name)
    return body


def test_response_body_is_identical_with_shadow_mode_on_and_a_header_links_the_records():
    with TestClient(build(FakeProvider("active"))) as plain:
        baseline = plain.post("/v1/generate", json=request_payload())

    app = build(
        FakeProvider("active"), [GatedProvider("s-one"), RaisingProvider(RuntimeError("x"), "boom")]
    )
    with TestClient(app) as client:
        shadowed = client.post("/v1/generate", json=request_payload())

        assert shadowed.status_code == baseline.status_code == 200
        assert stable_body(shadowed) == stable_body(baseline)
        assert COMPARISON_ID_HEADER not in baseline.headers
        comparison_id = shadowed.headers[COMPARISON_ID_HEADER]
        assert comparison_id.startswith("cmp-") and comparison_id not in shadowed.text

        store = app.state.service.shadow.store
        deadline = time.monotonic() + 2
        while not (snap := store.get(comparison_id)).complete and time.monotonic() < deadline:
            time.sleep(0.01)
        assert snap.complete and len(snap.shadows) == 2


def test_failures_keep_their_status_and_still_carry_the_comparison_header():
    app = build(RaisingProvider(RuntimeError("x"), "active"), [GatedProvider("s")])
    with TestClient(app) as client:
        response = client.post("/v1/generate", json=request_payload())

    assert response.status_code == 502 and response.json()["success"] is False
    assert response.headers[COMPARISON_ID_HEADER].startswith("cmp-")


def test_selector_errors_and_invalid_requests_launch_no_shadows_and_have_no_header():
    shadow = GatedProvider("s")
    with TestClient(build(FakeProvider("active"), [shadow])) as client:
        unknown = client.post("/v1/generate?provider=nope", json=request_payload())
        invalid = client.post("/v1/generate", json={"nonsense": True})

    assert (unknown.status_code, invalid.status_code) == (404, 422)
    assert COMPARISON_ID_HEADER not in unknown.headers
    assert COMPARISON_ID_HEADER not in invalid.headers
    assert shadow.calls == 0


def test_a_slow_shadow_does_not_slow_the_http_response_and_is_cancelled_at_shutdown():
    never = asyncio.Event()
    slow = GatedProvider("slow", gate=never)
    app = build(FakeProvider("active"), [slow])

    with TestClient(app) as client:
        started = time.perf_counter()
        response = client.post("/v1/generate", json=request_payload())
        elapsed = time.perf_counter() - started
        deadline = time.monotonic() + 2
        while slow.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert slow.calls == 1
        assert app.state.service.shadow.pending == 1

    assert response.status_code == 200 and elapsed < 0.5
    assert slow.cancelled is True
    assert app.state.service.shadow.pending == 0


def test_shutdown_ends_shadow_calls_before_closing_providers():
    seen = {}

    class ClosingProvider(GatedProvider):
        async def aclose(self):
            seen["pending_at_close"] = app.state.service.shadow.pending
            seen["shadow_cancelled_at_close"] = self.cancelled

    shadow = ClosingProvider("shadow", gate=asyncio.Event())
    app = build(FakeProvider("active"), [shadow])
    with TestClient(app) as client:
        client.post("/v1/generate", json=request_payload())
        deadline = time.monotonic() + 2
        while shadow.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert seen == {"pending_at_close": 0, "shadow_cancelled_at_close": True}


def test_providers_are_still_closed_if_draining_shadows_is_cancelled():
    closed = []

    class ClosingProvider(FakeProvider):
        async def aclose(self):
            closed.append(self.provider_id)

    app = build(ClosingProvider("active"), [FakeProvider("shadow")])

    async def cancelled_drain():
        raise asyncio.CancelledError

    app.state.service.aclose = cancelled_drain
    try:
        with TestClient(app):
            pass
    except BaseException:  # the lifespan surfaces the cancellation
        pass

    assert closed == ["active"]


def test_config_lists_shadow_targets_only_when_enabled():
    with TestClient(build(FakeProvider("active"))) as off:
        assert "shadow" not in off.get("/v1/config").json()

    settings = DirectorSettings.from_env(
        {
            "DIRECTOR_DEFAULT_PROVIDER": "rules-baseline",
            "DIRECTOR_SHADOW_TARGETS": f"groq:openai/gpt-oss-20b,BAD,{CANARY}",
        }
    )
    with TestClient(create_app(settings, default_registry())) as on:
        body = on.get("/v1/config").json()

    assert body["shadow"] == {
        "targets": [{"provider": "groq", "model": "openai/gpt-oss-20b"}],
        "rejected_config_entries": 2,
    }
    assert CANARY not in str(body)


def test_malformed_and_unavailable_shadow_config_still_boots_and_serves(caplog):
    caplog.set_level(logging.DEBUG)
    settings = DirectorSettings.from_env(
        {
            "DIRECTOR_SHADOW_TARGETS": f"groq,cerebras,no-such-provider,??{CANARY}",
            "DIRECTOR_SHADOW_MAX_IN_FLIGHT": "lots",
        }
    )

    with TestClient(create_app(settings, default_registry())) as client:
        response = client.post("/v1/generate", json=request_payload())

    assert response.status_code == 200 and response.json()["success"] is True
    assert response.json()["metadata"]["provider"] == "rules-baseline"
    assert CANARY not in caplog.text and CANARY not in response.text
