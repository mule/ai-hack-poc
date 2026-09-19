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


def test_default_registry_offers_only_the_offline_rules_provider():
    registry = default_registry()

    assert [d.id for d in registry.describe()] == ["rules-baseline"]
    assert registry.describe()[0].available is True


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
