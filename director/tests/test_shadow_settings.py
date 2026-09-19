"""DIRECTOR_SHADOW_* parsing: strict shapes, lenient failure, no secret echo."""

from __future__ import annotations

import logging

import pytest

from dungeon_director.errors import DirectorConfigError
from dungeon_director.settings import (
    MAX_SHADOW_TARGETS,
    DirectorSettings,
    ShadowSettings,
    ShadowTarget,
)

CANARY = "sk-live-canary-7f3a9c2e"


def parse(**env: str) -> ShadowSettings:
    return ShadowSettings.from_env(env)


def test_shadow_mode_is_off_by_default_and_when_blank():
    for env in ({}, {"DIRECTOR_SHADOW_TARGETS": ""}, {"DIRECTOR_SHADOW_TARGETS": "  ,, "}):
        settings = ShadowSettings.from_env(env)
        assert settings.enabled is False and settings.targets == () and settings.rejected == 0


def test_targets_accept_bare_providers_and_provider_model_pairs():
    settings = parse(
        DIRECTOR_SHADOW_TARGETS=(
            " groq:openai/gpt-oss-20b , cerebras,cloudflare-jev:@cf/typesafe/jev,,"
        )
    )

    assert settings.targets == (
        ShadowTarget("groq", "openai/gpt-oss-20b"),
        ShadowTarget("cerebras", None),
        ShadowTarget("cloudflare-jev", "@cf/typesafe/jev"),
    )
    assert settings.enabled and settings.rejected == 0


def test_only_the_first_colon_separates_provider_from_model():
    (target,) = parse(DIRECTOR_SHADOW_TARGETS="local:llama3:8b-q4").targets

    assert target == ShadowTarget("local", "llama3:8b-q4")


@pytest.mark.parametrize(
    "entry",
    [
        "UPPER",  # provider ids are lowercase
        "has space",
        "groq:",  # colon with no model
        ":model",  # no provider
        "groq:bad model",
        "-leading-dash",
        "a" * 65,
        "groq:" + "m" * 129,
        "groq/openai",  # slash belongs to models, not providers
        "http://evil.example",
    ],
)
def test_malformed_entries_are_rejected_and_counted_without_failing(entry):
    settings = parse(DIRECTOR_SHADOW_TARGETS=f"good,{entry}")

    assert settings.targets == (ShadowTarget("good"),)
    assert settings.rejected == 1


def test_duplicates_and_the_target_cap_are_enforced():
    entries = [f"p{i}" for i in range(MAX_SHADOW_TARGETS + 3)]
    settings = parse(DIRECTOR_SHADOW_TARGETS=",".join(["p0", *entries]))

    assert len(settings.targets) == MAX_SHADOW_TARGETS
    assert settings.targets[0] == ShadowTarget("p0")
    assert settings.rejected == 1 + 3  # one duplicate, three over the cap


def test_malformed_targets_are_never_logged_or_repr_d(caplog):
    caplog.set_level(logging.DEBUG)

    settings = parse(DIRECTOR_SHADOW_TARGETS=f"groq,{CANARY} oops,Bad:{CANARY}")

    assert settings.targets == (ShadowTarget("groq"),) and settings.rejected == 2
    assert CANARY not in caplog.text and CANARY not in repr(settings)
    assert "DIRECTOR_SHADOW_TARGETS" in caplog.text, "the warning names the variable"


def test_numeric_defaults():
    settings = parse()

    assert settings.timeout_seconds is None, "None means: use the director timeout"
    assert (settings.max_in_flight, settings.store_size, settings.drain_seconds) == (8, 128, 5.0)


def test_numeric_values_are_read():
    settings = parse(
        DIRECTOR_SHADOW_TIMEOUT_SECONDS="2.5",
        DIRECTOR_SHADOW_MAX_IN_FLIGHT="3",
        DIRECTOR_SHADOW_STORE_SIZE="10",
        DIRECTOR_SHADOW_DRAIN_SECONDS="0",
    )

    assert settings.timeout_seconds == 2.5
    assert (settings.max_in_flight, settings.store_size, settings.drain_seconds) == (3, 10, 0.0)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DIRECTOR_SHADOW_TIMEOUT_SECONDS", v)
        for v in ("0", "-1", "301", "nan", "inf", "soon", CANARY)
    ]
    + [("DIRECTOR_SHADOW_MAX_IN_FLIGHT", v) for v in ("0", "65", "1.5", "many", "-3", CANARY)]
    + [("DIRECTOR_SHADOW_STORE_SIZE", v) for v in ("0", "1025", "1e3", "lots", CANARY)]
    + [("DIRECTOR_SHADOW_DRAIN_SECONDS", v) for v in ("-1", "61", "nan", "inf", "x", CANARY)],
)
def test_bad_numbers_fall_back_to_defaults_with_a_value_free_warning(name, value, caplog):
    caplog.set_level(logging.DEBUG)

    settings = ShadowSettings.from_env({name: value})

    assert settings == ShadowSettings()
    assert name in caplog.text
    if len(value) > 4:  # short values like "0" collide with the wording of the warning
        assert value not in caplog.text, "the offending value must not be echoed"


def test_director_settings_reads_shadow_config_but_shadow_typos_never_block_startup():
    settings = DirectorSettings.from_env(
        {
            "DIRECTOR_SHADOW_TARGETS": "groq,???",
            "DIRECTOR_SHADOW_STORE_SIZE": "nope",
        }
    )

    assert settings.shadow.targets == (ShadowTarget("groq"),)
    assert settings.shadow.store_size == 128 and settings.shadow.rejected == 1
    with pytest.raises(DirectorConfigError):  # the core timeout stays strict
        DirectorSettings.from_env(
            {"DIRECTOR_TIMEOUT_SECONDS": "abc", "DIRECTOR_SHADOW_TARGETS": "groq"}
        )


def test_default_director_settings_have_shadow_off():
    assert DirectorSettings().shadow.enabled is False
