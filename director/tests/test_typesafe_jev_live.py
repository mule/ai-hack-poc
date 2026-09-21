"""Opt-in, billable smoke test for TypeSafe's direct Jev endpoint.

Run with:

    RUN_LIVE_TYPESAFE_JEV=1 TYPESAFE_API_KEY=... \
      python -m pytest -m live_typesafe_jev tests/test_typesafe_jev_live.py -v
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fakes import make_request

from dungeon_director.contracts import RoomPlan
from dungeon_director.typesafe_jev import TypeSafeJevConfig, TypeSafeJevProvider

_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()
_OPT_IN = os.environ.get("RUN_LIVE_TYPESAFE_JEV", "").strip() == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.live_typesafe_jev,
    pytest.mark.skipif(
        not (_OPT_IN and _KEY),
        reason="set RUN_LIVE_TYPESAFE_JEV=1 and TYPESAFE_API_KEY to opt in",
    ),
]


def test_live_direct_typesafe_returns_a_valid_room_plan():
    provider = TypeSafeJevProvider(TypeSafeJevConfig.from_env())

    async def generate_and_close():
        try:
            return await provider.generate(make_request(), model=provider.default_model)
        finally:
            await provider.aclose()

    result = asyncio.run(generate_and_close())

    assert isinstance(result.payload, RoomPlan)
    assert result.payload.room_id.startswith("jev-")
    assert result.provider_metadata["jev_model"]
    assert result.usage is None or result.usage.input_tokens is not None
