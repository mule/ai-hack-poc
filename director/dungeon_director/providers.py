"""The provider abstraction every dungeon director backend implements.

A provider turns a canonical :class:`GenerationRequest` into a room decision.
It knows nothing about HTTP, timeouts or response envelopes: the service owns
those, so every provider gets identical timeout, cancellation, validation and
failure handling and latency numbers stay comparable across providers.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import JsonValue

from dungeon_director.contracts import GenerationRequest, RoomPlan, UsageStats

__all__ = [
    "MODEL_ID_RE",
    "PROVIDER_ID_RE",
    "DungeonDirectorProvider",
    "PlanPayload",
    "ProviderAvailability",
    "ProviderResult",
]

#: Stable provider ids: lowercase, start with a letter or digit, at most 64 chars.
PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
#: Model ids follow each vendor's naming (``openai/gpt-oss-20b``, ``@cf/...``).
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}$")

#: What a provider may hand back as its decision. A ready :class:`RoomPlan`, a
#: decoded JSON object, or raw JSON text/bytes are all accepted; the service
#: validates anything that is not already a ``RoomPlan`` against the contract.
PlanPayload = RoomPlan | Mapping[str, Any] | str | bytes


@dataclass(frozen=True, slots=True)
class ProviderAvailability:
    """Whether a provider can serve requests right now.

    ``reason`` is for operators (logs); it is never sent over the API.
    """

    available: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """A provider's decision plus the telemetry it can report."""

    payload: PlanPayload
    usage: UsageStats | None = None
    provider_metadata: dict[str, JsonValue] = field(default_factory=dict)


class DungeonDirectorProvider(ABC):
    """Base class for dungeon director providers.

    Subclasses set ``provider_id`` (stable, lowercase, used by the game to
    select the provider), ``models`` and ``default_model`` as class or instance
    attributes. Ids are validated when the provider is registered.
    """

    provider_id: str
    models: tuple[str, ...]
    default_model: str

    @property
    def availability(self) -> ProviderAvailability:
        """Override to report missing credentials or an unreachable backend."""
        return ProviderAvailability(available=True)

    @abstractmethod
    async def generate(self, request: GenerationRequest, *, model: str) -> ProviderResult:
        """Decide the room behind ``request.target_exit``.

        Must be genuinely async and cancellation-safe. The service cancels the
        awaiting task on timeout or when the request task itself is cancelled, so
        never swallow ``CancelledError`` and never retry internally. Never block
        the event loop (``time.sleep``, a synchronous HTTP client): a blocking
        call cannot be interrupted, stalls all other requests, and is only
        flagged as a timeout after it returns. Raise
        :class:`~dungeon_director.errors.ProviderError` for failures you can
        classify (only its ``code`` reaches the game); any other exception is
        reported as a generic ``provider_error``.
        """
