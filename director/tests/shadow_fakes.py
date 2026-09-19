"""Doubles and scenario helpers for the shadow-evaluation tests."""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from fakes import FakeProvider, valid_room_dict

from dungeon_director.contracts import GenerationRequest, RoomPlan, UsageStats
from dungeon_director.providers import DungeonDirectorProvider, ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget
from dungeon_director.shadow import ShadowObserver

CANARY = "sk-live-canary-7f3a9c2e"


class GatedProvider(FakeProvider):
    """Records every request it gets, then waits on ``gate`` (if any) before answering.

    Each provider answers with its own room id, so a test can tell whose room
    reached the game.
    """

    def __init__(
        self,
        provider_id: str,
        *,
        gate: asyncio.Event | None = None,
        models: tuple[str, ...] = ("fake-model",),
    ) -> None:
        super().__init__(provider_id, models=models)
        self.gate = gate
        self.started = asyncio.Event()
        self.cancelled = False
        self.requests: list[GenerationRequest] = []

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        self.requests.append(request)
        self.started.set()
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        room = valid_room_dict(request)
        room["room_id"] = f"{self.provider_id}-room"
        return ProviderResult(
            payload=RoomPlan.model_validate(room),
            usage=UsageStats(input_tokens=11, output_tokens=7, estimated_cost_usd=0.001),
            provider_metadata={"served_by": self.provider_id},
        )


class MutatingProvider(GatedProvider):
    """A misbehaving shadow that scribbles on the request it was handed."""

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        request.state.player.hp = 1
        request.state.depth = 99
        request.state.recent_events.clear()
        request.request_id = "tampered"
        return await super().decide(request)


class StubbornProvider(GatedProvider):
    """Swallows the first cancellation and keeps waiting on ``release``."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id, gate=asyncio.Event())
        self.release = asyncio.Event()
        self.swallowed = 0

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        self.started.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.swallowed += 1
        await self.release.wait()
        return await FakeProvider.decide(self, request)


class RecordingObserver(ShadowObserver):
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.finished: list[Any] = []

    def comparison_started(self, comparison: Any) -> None:
        self.started.append(comparison)

    def execution_finished(self, record: Any) -> None:
        self.finished.append(record)


class ExplodingObserver(ShadowObserver):
    def __init__(self, message: str = "observer blew up") -> None:
        self.message = message
        self.calls = 0

    def comparison_started(self, comparison: Any) -> None:
        self.calls += 1
        raise RuntimeError(self.message)

    def execution_finished(self, record: Any) -> None:
        self.calls += 1
        raise RuntimeError(self.message)


def targets_for(*providers: DungeonDirectorProvider) -> tuple[ShadowTarget, ...]:
    return tuple(ShadowTarget(p.provider_id) for p in providers)


def make_shadow_service(
    active: DungeonDirectorProvider,
    shadows: Sequence[DungeonDirectorProvider] = (),
    *,
    targets: Sequence[ShadowTarget] | None = None,
    timeout: float = 5.0,
    shadow_timeout: float | None = None,
    max_in_flight: int = 8,
    store_size: int = 128,
    drain_seconds: float = 0.5,
    observers: Sequence[ShadowObserver] = (),
    extra: Sequence[DungeonDirectorProvider] = (),
) -> DirectorService:
    registry = ProviderRegistry()
    for provider in (active, *shadows, *extra):
        registry.register(provider)
    settings = DirectorSettings(
        default_provider=active.provider_id,
        timeout_seconds=timeout,
        shadow=ShadowSettings(
            targets=tuple(targets) if targets is not None else targets_for(*shadows),
            timeout_seconds=shadow_timeout,
            max_in_flight=max_in_flight,
            store_size=store_size,
            drain_seconds=drain_seconds,
        ),
    )
    return DirectorService(registry, settings, shadow_observers=observers)


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.005)


def run_scenario(scenario: Callable[[], Awaitable[Any]]) -> Any:
    """``asyncio.run`` a scenario and fail on any loop-level unhandled exception.

    A background task whose exception nobody retrieved surfaces here (through
    the loop's exception handler, triggered by a forced garbage collection), so
    "background exceptions are consumed" is checked for every scenario.
    """
    contexts: list[dict[str, Any]] = []

    async def main() -> Any:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, ctx: contexts.append(ctx))
        try:
            return await scenario()
        finally:
            gc.collect()
            await asyncio.sleep(0)

    result = asyncio.run(main())
    assert contexts == [], f"unhandled asyncio errors: {contexts}"
    return result
