"""Live Cerebras integration tests (issue #10).

These make REAL, billable calls to Cerebras Inference's chat completions endpoint
and must never run as part of the normal offline suite. They require both an
explicit ``RUN_LIVE_CEREBRAS=1`` opt-in and credentials, and they never print
credentials, headers, URLs or upstream bodies.

Every test carries ``pytest.mark.live`` (the suite-wide marker for tests that may
reach the network, which the offline guard exempts) as well as the provider
marker ``live_cerebras``.

Run explicitly with credentials:

    export RUN_LIVE_CEREBRAS=1 CEREBRAS_API_KEY=...
    python -m pytest -m live_cerebras tests/test_cerebras_live.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fakes import make_request

from dungeon_director.cerebras import CerebrasConfig, CerebrasProvider
from dungeon_director.contracts import ExitDirection, GenerationRequest, RoomPlan
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

_KEY = os.environ.get("CEREBRAS_API_KEY", "").strip()
_LIVE_OPT_IN = os.environ.get("RUN_LIVE_CEREBRAS", "").strip() == "1"

#: The adapter has no timeout of its own, so a stuck live call must not hang CI.
_LIVE_DEADLINE_SECONDS = 30

pytestmark = [
    pytest.mark.live,
    pytest.mark.live_cerebras,
    pytest.mark.skipif(
        not (_LIVE_OPT_IN and _KEY),
        reason="set RUN_LIVE_CEREBRAS=1 plus CEREBRAS_API_KEY to opt into live Cerebras calls",
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


def _provider() -> CerebrasProvider:
    return CerebrasProvider(CerebrasConfig.from_env())


def test_live_configuration_is_detected():
    assert _provider().availability.available is True


def test_live_provider_returns_raw_json_that_is_a_valid_room_plan():
    request = make_request()

    async def run() -> ProviderResult:
        provider = _provider()
        try:
            async with asyncio.timeout(_LIVE_DEADLINE_SECONDS):
                return await provider.generate(request, model=provider.default_model)
        finally:
            await provider.aclose()

    result = asyncio.run(run())

    assert isinstance(result.payload, str)
    _assert_valid_room(RoomPlan.model_validate_json(result.payload), request)
    assert result.usage is not None and result.usage.input_tokens is not None
    assert result.provider_metadata.get("cerebras_model")
    assert result.provider_metadata.get("finish_reason") == "stop"


def test_live_end_to_end_through_the_director_service():
    request = make_request()
    provider = _provider()
    registry = ProviderRegistry()
    registry.register(provider)
    service = DirectorService(
        registry,
        DirectorSettings(
            default_provider="cerebras",
            default_model=provider.default_model,
            timeout_seconds=_LIVE_DEADLINE_SECONDS,
        ),
    )

    async def run():
        try:
            return await service.generate(request)
        finally:
            await registry.aclose()

    outcome = asyncio.run(run())

    assert outcome.status_code == 200, outcome.response.metadata.error
    assert outcome.response.room is not None
    _assert_valid_room(outcome.response.room, request)
    assert outcome.response.metadata.usage is not None
    assert outcome.response.metadata.provider == "cerebras"


def _assert_valid_room(room: RoomPlan, request: GenerationRequest) -> None:
    assert room.depth == request.state.depth
    directions = [exit_.direction for exit_ in room.exits]
    assert _OPPOSITE[request.target_exit.direction] in directions, (
        "the room must connect back to its frontier"
    )
    assert len(directions) == len(set(directions))
