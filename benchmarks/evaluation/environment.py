"""Provider selection and the version/configuration evidence recorded with a run.

Configuration is read from the same ``*Config.from_env`` objects the providers run
with, so what is recorded is what was used. Secrets never enter the record: a
field is dropped when the config marks it ``repr=False`` (the repository's own
convention for credentials) or when its name looks like one, and URLs are cut
down to their origin. Custom URL paths can contain tenant-specific secrets.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import platform
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dungeon_director.cerebras import (
    CEREBRAS_PROVIDER_ID,
    CerebrasConfig,
    CerebrasProvider,
)
from dungeon_director.cloudflare_jev import (
    CLOUDFLARE_JEV_PROVIDER_ID,
    CloudflareJevProvider,
    JevConfig,
)
from dungeon_director.contracts import CONTRACT_VERSION
from dungeon_director.errors import DirectorConfigError
from dungeon_director.groq import GROQ_PROVIDER_ID, GroqConfig, GroqProvider
from dungeon_director.providers import DungeonDirectorProvider
from dungeon_director.rules import RULES_MODEL, RULES_PROVIDER_ID, RulesProvider
from dungeon_director.typesafe_jev import (
    TYPESAFE_JEV_PROVIDER_ID,
    TypeSafeJevConfig,
    TypeSafeJevProvider,
)

# `max_completion_tokens` is a budget, not a credential: only *_token/*_key names match.
_SECRET_NAME_RE = re.compile(r"(^|_)(key|token)$|secret|password|account_id", re.IGNORECASE)
_PACKAGES = (
    "dungeon-director",
    "pydantic",
    "httpx",
    "fastapi",
    "jsonschema",
    "opentelemetry-sdk",
)

# provider id -> (config class, provider class)
HOSTED: dict[str, tuple[type, type]] = {
    TYPESAFE_JEV_PROVIDER_ID: (TypeSafeJevConfig, TypeSafeJevProvider),
    CLOUDFLARE_JEV_PROVIDER_ID: (JevConfig, CloudflareJevProvider),
    GROQ_PROVIDER_ID: (GroqConfig, GroqProvider),
    CEREBRAS_PROVIDER_ID: (CerebrasConfig, CerebrasProvider),
}
SUPPORTED_PROVIDERS = (RULES_PROVIDER_ID, *HOSTED)


class SelectionError(ValueError):
    """A provider/model selection cannot be used for this run."""


@dataclass(frozen=True)
class Selection:
    """One provider/model pair under test, with the exact config it will run with."""

    provider: str
    model: str
    config: Any = None  # the provider's config dataclass; None for the rules baseline

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def is_live(self) -> bool:
        return self.provider != RULES_PROVIDER_ID

    def build_provider(self) -> DungeonDirectorProvider:
        if not self.is_live:
            return RulesProvider()
        return HOSTED[self.provider][1](self.config)  # type: ignore[no-any-return]


def resolve_selection(spec: str, environ: Mapping[str, str]) -> Selection:
    """Turn ``provider`` or ``provider:model`` into a Selection.

    A bare provider uses the model its environment configuration names (or its
    default); an explicit model replaces it. Either way the returned config is the
    one the provider will run with.
    """
    provider, _, model = spec.partition(":")
    if provider not in SUPPORTED_PROVIDERS:
        raise SelectionError(f"unknown provider {provider!r}; choose from {SUPPORTED_PROVIDERS}")
    if provider == RULES_PROVIDER_ID:
        if model and model != RULES_MODEL:
            raise SelectionError(f"{RULES_PROVIDER_ID} only has the model {RULES_MODEL!r}")
        return Selection(RULES_PROVIDER_ID, RULES_MODEL)
    config_cls = HOSTED[provider][0]
    try:
        config = config_cls.from_env(environ)  # type: ignore[attr-defined]
        if model:
            config = dataclasses.replace(config, model=model)
    except DirectorConfigError as exc:
        raise SelectionError(f"invalid {provider} configuration ({type(exc).__name__})") from exc
    return Selection(provider, config.model, config)


def check_usable(selection: Selection) -> None:
    """Raise unless the provider reports itself available (credentials present)."""
    availability = selection.build_provider().availability
    if not availability.available:
        raise SelectionError(f"{selection.label} is not usable: {availability.reason}")


def _safe_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        parsed_port = parts.port
    except ValueError:
        parsed_port = None
    port = f":{parsed_port}" if parsed_port is not None else ""
    return f"{parts.scheme}://{host}{port}"


def describe_config(config: Any) -> dict[str, Any]:
    """Non-secret configuration fields, plus which fields were withheld."""
    if config is None:
        return {"fields": {}, "withheld": [], "credentials_present": False}
    fields: dict[str, Any] = {}
    withheld: list[str] = []
    credentials = False
    for f in dataclasses.fields(config):
        value = getattr(config, f.name)
        if not f.repr or _SECRET_NAME_RE.search(f.name):
            withheld.append(f.name)
            credentials = credentials or bool(value)
        elif isinstance(value, str) and f.name.endswith("url"):
            fields[f.name] = _safe_url(value)
        else:
            fields[f.name] = value
    return {"fields": fields, "withheld": sorted(withheld), "credentials_present": credentials}


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def capture_environment(
    selections: list[Selection],
    *,
    repo_root: Path,
    vantage_point: str | None,
    timeout_seconds: float,
    godot_version: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Versions and configuration that a later reader needs to interpret the numbers."""
    status = _git(repo_root, "status", "--porcelain")
    providers = []
    for selection in selections:
        providers.append(
            {
                "provider": selection.provider,
                "model": selection.model,
                "live": selection.is_live,
                "config": describe_config(selection.config),
            }
        )
    return {
        "code": {
            "git_commit": _git(repo_root, "rev-parse", "HEAD"),
            "git_dirty": None if status is None else bool(status),
        },
        "runtime": {
            "python": sys.version.split()[0],
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "packages": _package_versions(),
        },
        "contract_version": CONTRACT_VERSION,
        "director": {"timeout_seconds": timeout_seconds, "retries": 0},
        "godot": godot_version() if godot_version else None,
        "vantage_point": vantage_point,
        "providers": providers,
    }
