"""Provider registry: stable ids, explicit availability, safe public description.

The registry is the only place providers are looked up. It hands the service a
provider plus a concrete model, and hands the API a description that contains
identifiers and availability flags only: no credentials, endpoints or
operator-facing availability reasons. It also owns provider shutdown:
:meth:`ProviderRegistry.aclose` closes every registered provider that exposes
an ``aclose`` coroutine (duck-typed), exactly once per provider, so async
resources such as HTTP clients are released deterministically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from dungeon_director.cerebras import CerebrasProvider
from dungeon_director.cloudflare_jev import CloudflareJevProvider
from dungeon_director.errors import (
    DirectorConfigError,
    ProviderSelectionError,
    SelectionReason,
)
from dungeon_director.groq import GroqProvider
from dungeon_director.providers import (
    MODEL_ID_RE,
    PROVIDER_ID_RE,
    DungeonDirectorProvider,
    ProviderAvailability,
)
from dungeon_director.rules import RulesProvider

__all__ = ["ProviderDescriptor", "ProviderRegistry", "ProviderSelection", "default_registry"]

logger = logging.getLogger(__name__)

_PROVIDER_ID_RE = PROVIDER_ID_RE
_MODEL_ID_RE = MODEL_ID_RE


class ProviderDescriptor(BaseModel):
    """Public, secret-free description of one registered provider."""

    model_config = ConfigDict(frozen=True)

    id: str
    available: bool
    default_model: str
    models: list[str]


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    provider: DungeonDirectorProvider
    model: str


def _check_availability(provider: DungeonDirectorProvider) -> ProviderAvailability:
    """Ask a provider whether it is usable; a misbehaving check means "no".

    Availability checks run adapter code on every ``/v1/config`` and
    ``/v1/generate`` call, so an exception (or a nonsense return value) must
    degrade that one provider to unavailable instead of failing the request.
    Only ``Exception`` is caught: cancellation and interrupts propagate.
    """
    try:
        availability = provider.availability
        return ProviderAvailability(bool(availability.available), availability.reason)
    except Exception as exc:
        # Type only: exception text and tracebacks can carry credentials.
        logger.error(
            "availability check for provider %s failed: %s",
            provider.provider_id,
            type(exc).__name__,
        )
        return ProviderAvailability(False, "availability check raised an exception")


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, DungeonDirectorProvider] = {}

    def register(self, provider: DungeonDirectorProvider) -> None:
        provider_id = provider.provider_id
        if not isinstance(provider_id, str) or not _PROVIDER_ID_RE.fullmatch(provider_id):
            raise DirectorConfigError(
                f"invalid provider id {provider_id!r}: use lowercase letters, digits, "
                "'_', '.' or '-' (1-64 chars, starting with a letter or digit)"
            )
        if provider_id in self._providers:
            raise DirectorConfigError(f"provider id {provider_id!r} is already registered")
        if not provider.models:
            raise DirectorConfigError(f"provider {provider_id!r} must declare at least one model")
        for model in provider.models:
            if not isinstance(model, str) or not _MODEL_ID_RE.fullmatch(model):
                raise DirectorConfigError(
                    f"provider {provider_id!r} has invalid model id {model!r}"
                )
        if provider.default_model not in provider.models:
            raise DirectorConfigError(
                f"provider {provider_id!r}: default_model {provider.default_model!r} "
                f"is not in models {list(provider.models)}"
            )
        self._providers[provider_id] = provider

    def select(self, provider_id: str, model: str | None) -> ProviderSelection:
        """Resolve a provider/model pair or raise :class:`ProviderSelectionError`.

        Order matters for the caller's diagnosis: unknown provider, then
        unavailable provider, then unknown model.
        """
        provider = self._providers.get(provider_id)
        if provider is None:
            known = ", ".join(self._providers) or "none"
            raise ProviderSelectionError(
                SelectionReason.UNKNOWN_PROVIDER,
                f"Unknown provider {provider_id!r}. Registered providers: {known}.",
            )
        availability = _check_availability(provider)
        if not availability.available:
            # The reason may mention configuration; keep it in the log only.
            logger.warning("provider %s unavailable: %s", provider_id, availability.reason)
            raise ProviderSelectionError(
                SelectionReason.PROVIDER_UNAVAILABLE,
                f"Provider {provider_id!r} is registered but currently unavailable.",
            )
        chosen = model if model is not None else provider.default_model
        if chosen not in provider.models:
            raise ProviderSelectionError(
                SelectionReason.UNKNOWN_MODEL,
                f"Unknown model {chosen!r} for provider {provider_id!r}. "
                f"Available models: {', '.join(provider.models)}.",
            )
        return ProviderSelection(provider=provider, model=chosen)

    def is_registered(self, provider_id: str, model: str | None) -> bool:
        """Whether this exact provider (and model, when given) is registered.

        Ignores availability. Meant for bounding metric label values: an id
        that comes from a request or from operator config is only safe to use
        as a label when the registry vouches for it.
        """
        provider = self._providers.get(provider_id)
        return provider is not None and (model is None or model in provider.models)

    def describe(self) -> list[ProviderDescriptor]:
        return [
            ProviderDescriptor(
                id=provider.provider_id,
                available=_check_availability(provider).available,
                default_model=provider.default_model,
                models=list(provider.models),
            )
            for provider in self._providers.values()
        ]

    async def aclose(self) -> None:
        """Close every registered provider that exposes ``aclose``, once each.

        Shutdown must be as defensive as availability checks: a misbehaving
        closer degrades to a log line for that one provider while the rest are
        still closed. Only ``Exception`` is caught: cancellation and interrupts
        (``CancelledError``/``KeyboardInterrupt`` are ``BaseException``) are
        not caught here and propagate immediately, and providers after the
        cancelled one are *not* closed. Logged failures carry the provider id
        and the exception *type* only: close errors and tracebacks can echo
        credentials, same as every other adapter-controlled text.
        """
        closed_ids: set[int] = set()
        for provider_id, provider in self._providers.items():
            identity = id(provider)
            if identity in closed_ids:
                continue
            closed_ids.add(identity)
            closer = getattr(provider, "aclose", None)
            if not callable(closer):
                continue
            try:
                await closer()
            except Exception as exc:
                # Type only: exception text and tracebacks can carry credentials.
                logger.error(
                    "aclose for provider %s failed: %s (remaining providers still closed)",
                    provider_id,
                    type(exc).__name__,
                )


def default_registry() -> ProviderRegistry:
    """The registry the service starts with.

    The offline rules baseline is always available. The optional hosted
    providers (Cloudflare Jev, Groq, Cerebras) are always registered too, but report
    themselves unavailable until their credentials are configured, so a
    checkout without credentials keeps working with the offline default.
    """
    registry = ProviderRegistry()
    registry.register(RulesProvider())
    for provider_type in (CloudflareJevProvider, GroqProvider, CerebrasProvider):
        _register_optional(registry, provider_type)
    return registry


def _register_optional(
    registry: ProviderRegistry,
    provider_type: type[CloudflareJevProvider] | type[GroqProvider] | type[CerebrasProvider],
) -> None:
    """Register an environment-configured provider without letting it stop startup.

    These providers are optional while rules-baseline is the default. A typo in
    their environment (bad URL, model id, effort, token budget, key) must not
    take the offline service down: the provider is registered from an empty
    environment instead, so it reports unavailable and selecting it fails as
    unavailable. Only the exception type and provider id are logged, because
    configuration text can contain credential material.
    """
    try:
        registry.register(provider_type.from_env())
    except DirectorConfigError as exc:
        disabled = provider_type.from_env({})
        logger.warning(
            "invalid optional %s configuration; provider disabled (%s)",
            disabled.provider_id,
            type(exc).__name__,
        )
        registry.register(disabled)
