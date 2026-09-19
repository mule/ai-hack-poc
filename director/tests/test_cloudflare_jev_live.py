"""Live Cloudflare Jev integration tests (issue #8).

These make REAL, billable calls to Cloudflare Workers AI's run endpoint and
must never run as part of the normal offline suite. They require both an
explicit ``RUN_LIVE_JEV=1`` opt-in and credentials, and they never print
credentials, headers, URLs or upstream bodies.

Run explicitly with credentials:

    export RUN_LIVE_JEV=1 CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
    python -m pytest -m live tests/test_cloudflare_jev_live.py -v
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fakes import make_request

from dungeon_director.cloudflare_jev import (
    CloudflareJevProvider,
    JevConfig,
)
from dungeon_director.contracts import ExitDirection, RoomPlan
from dungeon_director.providers import ProviderResult

_ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
_LIVE_OPT_IN = os.environ.get("RUN_LIVE_JEV", "").strip() == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (_LIVE_OPT_IN and _ACCOUNT and _TOKEN),
        reason=(
            "set RUN_LIVE_JEV=1 plus CLOUDFLARE_ACCOUNT_ID and "
            "CLOUDFLARE_API_TOKEN to opt into live Jev calls"
        ),
    ),
]


@pytest.fixture
def provider() -> CloudflareJevProvider:
    return CloudflareJevProvider(JevConfig.from_env())


def test_live_configuration_is_detected(provider: CloudflareJevProvider):
    assert provider.availability.available is True


def test_live_generate_returns_a_schema_valid_room_plan(provider: CloudflareJevProvider):
    import asyncio

    async def generate_and_close() -> tuple[ProviderResult, Any]:
        request = make_request()
        try:
            return await provider.generate(request, model=provider.default_model), request
        finally:
            await provider.aclose()

    result, request = asyncio.run(generate_and_close())
    _assert_valid_result(result, request)


def _assert_valid_result(result: ProviderResult, request: Any) -> None:
    room = result.payload
    assert isinstance(room, RoomPlan)
    assert room.depth == request.state.depth
    assert room.room_id and room.room_id.startswith("jev-")
    directions = [exit_.direction for exit_ in room.exits]
    opposite = {
        ExitDirection.NORTH: ExitDirection.SOUTH,
        ExitDirection.SOUTH: ExitDirection.NORTH,
        ExitDirection.EAST: ExitDirection.WEST,
        ExitDirection.WEST: ExitDirection.EAST,
        ExitDirection.UP: ExitDirection.DOWN,
        ExitDirection.DOWN: ExitDirection.UP,
    }[request.target_exit.direction]
    assert opposite in directions, "the room must connect back to its frontier"
    assert len(directions) == len(set(directions))
    assert result.usage is None or result.usage.input_tokens is not None
    assert result.provider_metadata.get("jev_model", "")
