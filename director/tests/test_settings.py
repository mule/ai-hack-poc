"""Environment configuration: documented defaults, strict parsing, no secrets."""

from __future__ import annotations

import dataclasses

import pytest

from dungeon_director.errors import DirectorConfigError
from dungeon_director.settings import DirectorSettings


def test_defaults_when_environment_is_empty():
    settings = DirectorSettings.from_env({})

    assert settings.default_provider == "rules-baseline"
    assert settings.default_model is None
    assert settings.timeout_seconds == 10.0


def test_values_come_from_the_environment():
    settings = DirectorSettings.from_env(
        {
            "DIRECTOR_DEFAULT_PROVIDER": "groq",
            "DIRECTOR_DEFAULT_MODEL": "openai/gpt-oss-20b",
            "DIRECTOR_TIMEOUT_SECONDS": "2.5",
        }
    )

    assert settings.default_provider == "groq"
    assert settings.default_model == "openai/gpt-oss-20b"
    assert settings.timeout_seconds == 2.5


def test_blank_values_count_as_unset_so_a_copied_env_example_works():
    settings = DirectorSettings.from_env(
        {
            "DIRECTOR_DEFAULT_PROVIDER": "  ",
            "DIRECTOR_DEFAULT_MODEL": "",
            "DIRECTOR_TIMEOUT_SECONDS": "",
        }
    )

    assert settings == DirectorSettings.from_env({})


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "nan", "inf", "301"])
def test_invalid_timeout_names_the_variable(bad):
    with pytest.raises(DirectorConfigError, match="DIRECTOR_TIMEOUT_SECONDS"):
        DirectorSettings.from_env({"DIRECTOR_TIMEOUT_SECONDS": bad})


def test_provider_credentials_in_the_environment_are_never_captured():
    secret = "sk-live-do-not-leak"
    settings = DirectorSettings.from_env(
        {"GROQ_API_KEY": secret, "CEREBRAS_API_KEY": secret, "CLOUDFLARE_API_TOKEN": secret}
    )

    assert secret not in repr(settings)
    assert secret not in repr(dataclasses.asdict(settings))
    assert {f.name for f in dataclasses.fields(settings)} == {
        "default_provider",
        "default_model",
        "timeout_seconds",
        "shadow",  # provider/model ids and bounds only; see test_shadow_settings.py
    }
