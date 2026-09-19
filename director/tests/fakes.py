"""Test doubles and request builders shared by the director tests."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from typing import Any

from dungeon_director.contracts import ErrorKind, GenerationRequest, RoomPlan
from dungeon_director.errors import ProviderError
from dungeon_director.providers import (
    DungeonDirectorProvider,
    ProviderAvailability,
    ProviderResult,
)

FIXTURES = Path(__file__).resolve().parents[2] / "contracts" / "fixtures"


def request_payload(**overrides: Any) -> dict[str, Any]:
    """The canonical request fixture as a JSON-ready dict, with top-level overrides."""
    payload = json.loads((FIXTURES / "generation_request.json").read_text(encoding="utf-8"))
    payload = copy.deepcopy(payload)
    payload.update(overrides)
    return payload


def make_request(**overrides: Any) -> GenerationRequest:
    return GenerationRequest.model_validate(request_payload(**overrides))


def request_for_exit(
    direction: str,
    *,
    depth: int = 3,
    options: dict[str, Any] | None = None,
    **state_overrides: Any,
) -> GenerationRequest:
    """A request whose frontier is ``direction`` on room ``r-003``."""
    payload = request_payload()
    if options is not None:
        payload["options"] = options
    payload["state"]["depth"] = depth
    payload["state"].update(state_overrides)
    exit_ = {"room_id": "r-003", "direction": direction, "since_turn": 145}
    payload["state"]["unresolved_exits"] = [exit_]
    payload["target_exit"] = exit_
    return GenerationRequest.model_validate(payload)


def valid_room_dict(request: GenerationRequest) -> dict[str, Any]:
    """A schema-valid room for ``request`` (depth matches, back-link present)."""
    return {
        "room_id": "fake-room-1",
        "depth": request.state.depth,
        "room_type": "room",
        "size": "small",
        "danger": 1,
        "exits": [{"direction": "south", "kind": "door", "locked": False}],
    }


class FakeProvider(DungeonDirectorProvider):
    """Configurable fake: returns a valid room unless told otherwise."""

    def __init__(
        self,
        provider_id: str = "fake",
        *,
        models: tuple[str, ...] = ("fake-model",),
        available: bool = True,
    ) -> None:
        self.provider_id = provider_id
        self.models = models
        self.default_model = models[0]
        self._available = available
        self.calls = 0
        self.models_seen: list[str] = []

    @property
    def availability(self) -> ProviderAvailability:
        if self._available:
            return ProviderAvailability(True)
        return ProviderAvailability(False, "fake provider disabled for the test")

    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        self.calls += 1
        self.models_seen.append(model)
        return await self.decide(request)

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        return ProviderResult(payload=RoomPlan.model_validate(valid_room_dict(request)))


class SlowProvider(FakeProvider):
    """Blocks until cancelled, recording that it saw the cancellation."""

    def __init__(self, provider_id: str = "slow") -> None:
        super().__init__(provider_id)
        self.started = asyncio.Event()
        self.cancelled = False

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().decide(request)


class RaisingProvider(FakeProvider):
    def __init__(self, error: BaseException, provider_id: str = "broken") -> None:
        super().__init__(provider_id)
        self._error = error

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        raise self._error


class ClassifiedFailureProvider(RaisingProvider):
    def __init__(self, code: ErrorKind, message: str, provider_id: str = "classified") -> None:
        super().__init__(ProviderError(code, message), provider_id)


class MalformedProvider(FakeProvider):
    """Returns whatever payload it was given, valid or not."""

    def __init__(self, payload: Any, provider_id: str = "malformed") -> None:
        super().__init__(provider_id)
        self._payload = payload

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        return ProviderResult(payload=self._payload)


class BrokenAvailabilityProvider(FakeProvider):
    """Registered provider whose availability check itself blows up."""

    def __init__(self, error: BaseException | None = None, provider_id: str = "broken") -> None:
        super().__init__(provider_id)
        self._error = error if error is not None else RuntimeError("availability probe crashed")

    @property
    def availability(self) -> ProviderAvailability:
        raise self._error


class GarbageAvailabilityProvider(FakeProvider):
    """Availability check returns something that is not a ProviderAvailability."""

    @property
    def availability(self) -> ProviderAvailability:
        return None  # type: ignore[return-value]


class RawResultProvider(FakeProvider):
    """Returns ``value`` from ``generate`` untouched: a ProviderResult or something else."""

    def __init__(self, value: Any, provider_id: str = "raw") -> None:
        super().__init__(provider_id)
        self._value = value

    async def generate(self, request: GenerationRequest, *, model: str) -> Any:
        self.calls += 1
        return self._value


class SuppressingProvider(FakeProvider):
    """Swallows cancellation and returns a valid room anyway (a misbehaving adapter)."""

    def __init__(self, provider_id: str = "suppressing") -> None:
        super().__init__(provider_id)
        self.saw_cancel = False

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.saw_cancel = True
        return await super().decide(request)


class BlockingProvider(FakeProvider):
    """Blocks the event loop with a synchronous sleep, which cannot be interrupted."""

    def __init__(self, seconds: float, provider_id: str = "blocking") -> None:
        super().__init__(provider_id)
        self._seconds = seconds

    async def decide(self, request: GenerationRequest) -> ProviderResult:
        time.sleep(self._seconds)
        return await super().decide(request)
