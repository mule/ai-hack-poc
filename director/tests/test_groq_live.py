"""Live Groq integration tests (issue #9).

These make REAL, billable calls to Groq's chat-completions endpoint and must
never run as part of the normal offline suite. They run only when BOTH are
true: ``RUN_LIVE_GROQ=1`` (exactly ``1``) and ``GROQ_API_KEY`` is set. An
exported key alone never enables them, and the conftest offline guard scrubs
``GROQ_*`` from every non-``live`` test and refuses non-loopback sockets there.
The tests never print credentials, headers or upstream bodies.

Run explicitly:

    export RUN_LIVE_GROQ=1 GROQ_API_KEY=...
    python -m pytest -m live tests/test_groq_live.py -v

Cost: at most three short chat completions (about a thousand prompt tokens
each on the default ``openai/gpt-oss-20b``), plus one deliberately rejected
call that is not billed for tokens.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from fakes import make_request, request_for_exit

from dungeon_director.contracts import ErrorKind, ExitDirection, GenerationResponse, RoomType
from dungeon_director.groq import GroqConfig, GroqProvider
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

_KEY = os.environ.get("GROQ_API_KEY", "").strip()
_LIVE_OPT_IN = os.environ.get("RUN_LIVE_GROQ", "").strip() == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (_LIVE_OPT_IN and _KEY),
        reason="set RUN_LIVE_GROQ=1 plus GROQ_API_KEY to opt into live (billable) Groq calls",
    ),
]

_OPPOSITE = {
    ExitDirection.NORTH: ExitDirection.SOUTH,
    ExitDirection.SOUTH: ExitDirection.NORTH,
    ExitDirection.EAST: ExitDirection.WEST,
    ExitDirection.WEST: ExitDirection.EAST,
    ExitDirection.UP: ExitDirection.DOWN,
    ExitDirection.DOWN: ExitDirection.UP,
}


def _run_through_service(config: GroqConfig, request) -> GenerationResponse:
    """One generation through the real service (timeout, validation, envelope)."""

    async def run() -> GenerationResponse:
        provider = GroqProvider(config)
        registry = ProviderRegistry()
        registry.register(provider)
        service = DirectorService(
            registry, DirectorSettings(default_provider="groq", timeout_seconds=60.0)
        )
        try:
            return (await service.generate(request)).response
        finally:
            await provider.aclose()

    return asyncio.run(run())


def _assert_valid_success(response: GenerationResponse, request) -> None:
    error = response.metadata.error
    assert response.success, f"live call failed: {error.code.value if error else 'unknown'}"
    room = response.room
    assert room is not None
    assert room.depth == request.state.depth
    directions = [exit_.direction for exit_ in room.exits]
    assert _OPPOSITE[request.target_exit.direction] in directions, (
        "the room must connect back to its frontier"
    )
    assert len(directions) == len(set(directions))
    GenerationResponse.model_validate_json(response.model_dump_json())


def test_live_configuration_is_detected():
    assert GroqProvider.from_env().availability.available is True


def test_live_generate_returns_a_schema_valid_room_plan_with_telemetry():
    request = make_request()

    response = _run_through_service(GroqConfig.from_env(), request)

    _assert_valid_success(response, request)
    usage = response.metadata.usage
    assert usage is not None
    assert (usage.input_tokens or 0) > 0 and (usage.output_tokens or 0) > 0
    metadata = response.metadata.provider_metadata
    assert metadata["strict_schema"] is True
    assert metadata["reasoning_effort"] == "low"
    assert metadata["finish_reason"] == "stop"
    assert metadata["groq_request_id"]
    assert metadata["total_time_s"] > 0
    assert response.metadata.latency_ms and response.metadata.latency_ms > 0


def test_live_generate_honours_hard_generation_options():
    request = request_for_exit(
        "east",
        options={
            "max_danger": 1,
            "forbidden_room_types": ["vault", "treasure", "shop", "stairs_down"],
            "allow_secrets": False,
        },
    )

    response = _run_through_service(GroqConfig.from_env(), request)

    _assert_valid_success(response, request)
    room = response.room
    assert room.danger <= 1
    assert room.room_type not in {
        RoomType.VAULT,
        RoomType.TREASURE,
        RoomType.SHOP,
        RoomType.STAIRS_DOWN,
    }
    assert room.has_secret is not True and room.secret_probability == 0.0


def test_live_rejected_key_is_a_sanitized_provider_error():
    bad_key = "gsk_" + "x" * 40
    config = replace(GroqConfig.from_env(), api_key=bad_key)

    response = _run_through_service(config, make_request())

    assert response.success is False
    assert response.metadata.error.code is ErrorKind.PROVIDER_ERROR
    assert bad_key not in response.model_dump_json()
    assert _KEY not in response.model_dump_json()
