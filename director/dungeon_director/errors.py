"""Exceptions shared by the director's provider, registry and service layers."""

from __future__ import annotations

from enum import StrEnum

from dungeon_director.contracts import ErrorKind


class ProviderError(Exception):
    """A provider could not produce a usable decision.

    Adapters raise this to report a failure they understand (rate limit,
    upstream error, unparseable output). ``code`` becomes
    ``metadata.error.code`` in the canonical failure response.

    ``message`` and ``raw_excerpt`` are for adapter authors and tests only: the
    service treats adapters as an untrusted boundary and neither returns nor
    logs them (they can echo credentials). The game receives a stable public
    message chosen from ``code``.
    """

    def __init__(
        self,
        code: ErrorKind,
        message: str,
        *,
        raw_excerpt: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw_excerpt = raw_excerpt


class SelectionReason(StrEnum):
    """Why a provider/model selection was refused (stable, machine-readable)."""

    UNKNOWN_PROVIDER = "unknown_provider"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNKNOWN_MODEL = "unknown_model"


class ProviderSelectionError(Exception):
    """The requested provider/model is not registered or cannot be used now."""

    def __init__(self, reason: SelectionReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


class DirectorConfigError(ValueError):
    """Invalid director configuration; raised at startup, never per request."""
