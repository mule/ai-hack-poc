"""Provider registry: stable ids, explicit availability, default selection."""

from __future__ import annotations

import asyncio
import logging

import pytest
from fakes import BrokenAvailabilityProvider, FakeProvider, GarbageAvailabilityProvider

from dungeon_director.errors import (
    DirectorConfigError,
    ProviderSelectionError,
    SelectionReason,
)
from dungeon_director.registry import ProviderRegistry, default_registry


def registry_with(*providers: FakeProvider) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def test_default_registry_offers_rules_plus_unconfigured_cloudflare_jev(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    registry = default_registry()

    described = {d.id: d for d in registry.describe()}

    assert set(described) == {"rules-baseline", "cloudflare-jev"}
    assert described["rules-baseline"].available is True
    assert described["cloudflare-jev"].available is False
    assert registry.select("rules-baseline", None).model == "builtin-v1"


def test_invalid_optional_jev_environment_does_not_break_the_offline_default(monkeypatch, caplog):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "invalid account/id")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret-that-must-not-be-logged")

    with caplog.at_level(logging.WARNING):
        registry = default_registry()

    described = {item.id: item for item in registry.describe()}
    assert registry.select("rules-baseline", None).model == "builtin-v1"
    assert described["cloudflare-jev"].available is False
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-that-must-not-be-logged" not in logged
    assert "invalid account/id" not in logged


def test_select_returns_provider_and_its_default_model():
    provider = FakeProvider("alpha", models=("m1", "m2"))
    registry = registry_with(provider)

    selection = registry.select("alpha", None)

    assert selection.provider is provider
    assert selection.model == "m1"


def test_select_honours_an_explicit_known_model():
    registry = registry_with(FakeProvider("alpha", models=("m1", "m2")))

    assert registry.select("alpha", "m2").model == "m2"


def test_unknown_provider_is_refused_with_a_clear_reason():
    registry = registry_with(FakeProvider("alpha"))

    with pytest.raises(ProviderSelectionError) as info:
        registry.select("nope", None)

    assert info.value.reason is SelectionReason.UNKNOWN_PROVIDER
    assert "nope" in info.value.message
    assert "alpha" in info.value.message, "message should list the ids that do exist"


def test_unavailable_provider_is_refused_distinctly_from_unknown():
    registry = registry_with(FakeProvider("alpha"), FakeProvider("beta", available=False))

    with pytest.raises(ProviderSelectionError) as info:
        registry.select("beta", None)

    assert info.value.reason is SelectionReason.PROVIDER_UNAVAILABLE
    assert "fake provider disabled" not in info.value.message, (
        "operator-only availability reason must not reach clients"
    )


def test_unknown_model_is_refused_and_lists_known_models():
    registry = registry_with(FakeProvider("alpha", models=("m1", "m2")))

    with pytest.raises(ProviderSelectionError) as info:
        registry.select("alpha", "m9")

    assert info.value.reason is SelectionReason.UNKNOWN_MODEL
    assert "m1" in info.value.message and "m2" in info.value.message


def test_model_of_an_unavailable_provider_reports_unavailable_first():
    registry = registry_with(FakeProvider("beta", available=False))

    with pytest.raises(ProviderSelectionError) as info:
        registry.select("beta", "nonsense")

    assert info.value.reason is SelectionReason.PROVIDER_UNAVAILABLE


def test_describe_reports_ids_models_and_availability_only():
    registry = registry_with(
        FakeProvider("alpha", models=("m1", "m2")), FakeProvider("beta", available=False)
    )

    described = {d.id: d.model_dump() for d in registry.describe()}

    assert described == {
        "alpha": {
            "id": "alpha",
            "available": True,
            "default_model": "m1",
            "models": ["m1", "m2"],
        },
        "beta": {
            "id": "beta",
            "available": False,
            "default_model": "fake-model",
            "models": ["fake-model"],
        },
    }


def test_duplicate_provider_ids_are_rejected():
    registry = registry_with(FakeProvider("alpha"))

    with pytest.raises(DirectorConfigError, match="already registered"):
        registry.register(FakeProvider("alpha"))


@pytest.mark.parametrize("bad_id", ["", "Upper", "has space", "-lead", "x" * 65, "a/b"])
def test_provider_ids_must_be_stable_lowercase_slugs(bad_id):
    with pytest.raises(DirectorConfigError, match="provider id"):
        ProviderRegistry().register(FakeProvider(bad_id))


def test_provider_must_declare_its_default_model_among_its_models():
    provider = FakeProvider("alpha", models=("m1",))
    provider.default_model = "other"

    with pytest.raises(DirectorConfigError, match="default_model"):
        ProviderRegistry().register(provider)


def test_provider_must_declare_at_least_one_model():
    provider = FakeProvider("alpha")
    provider.models = ()

    with pytest.raises(DirectorConfigError, match="at least one model"):
        ProviderRegistry().register(provider)


def test_model_ids_may_use_provider_style_names():
    registry = registry_with(FakeProvider("groq", models=("openai/gpt-oss-20b", "@cf/x:1")))

    assert registry.select("groq", "openai/gpt-oss-20b").model == "openai/gpt-oss-20b"


def test_availability_check_that_raises_counts_as_unavailable_and_is_logged(caplog):
    registry = registry_with(FakeProvider("ok"), BrokenAvailabilityProvider())

    with caplog.at_level(logging.ERROR):
        described = {d.id: d.available for d in registry.describe()}
        with pytest.raises(ProviderSelectionError) as info:
            registry.select("broken", None)

    assert described == {"ok": True, "broken": False}
    assert info.value.reason is SelectionReason.PROVIDER_UNAVAILABLE
    assert "availability probe crashed" not in info.value.message
    logged = "\n".join(f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert "broken" in logged and "RuntimeError" in logged
    assert "availability probe crashed" not in logged, "exception text can carry secrets"
    assert not any(r.exc_info for r in caplog.records)
    assert registry.select("ok", None).provider.provider_id == "ok"


def test_availability_returning_garbage_counts_as_unavailable():
    registry = registry_with(GarbageAvailabilityProvider("odd"))

    assert registry.describe()[0].available is False


@pytest.mark.parametrize("cancel", [asyncio.CancelledError(), KeyboardInterrupt()])
def test_cancellation_and_interrupts_are_not_swallowed_by_the_availability_check(cancel):
    registry = registry_with(BrokenAvailabilityProvider(cancel))

    with pytest.raises(type(cancel)):
        registry.describe()
    with pytest.raises(type(cancel)):
        registry.select("broken", None)


class ClosingProvider(FakeProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        super().__init__(provider_id)
        self.close_error = close_error
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def test_registry_close_continues_after_a_provider_failure_without_logging_secret(caplog):
    secret = "close-error-secret"
    broken = ClosingProvider("broken-close", close_error=RuntimeError(secret))
    healthy = ClosingProvider("healthy-close")
    registry = registry_with(broken, healthy)

    with caplog.at_level(logging.ERROR):
        asyncio.run(registry.aclose())

    assert broken.close_calls == 1
    assert healthy.close_calls == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert secret not in logged
    assert not any(record.exc_info for record in caplog.records)


def test_registry_close_does_not_swallow_cancellation():
    cancelled = ClosingProvider("cancelled-close", close_error=asyncio.CancelledError())
    untouched = ClosingProvider("untouched-close")
    registry = registry_with(cancelled, untouched)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(registry.aclose())

    assert cancelled.close_calls == 1
    assert untouched.close_calls == 0
