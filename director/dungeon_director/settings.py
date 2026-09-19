"""Director configuration read from the environment.

Only non-secret service behaviour lives here. Provider credentials are never
part of :class:`DirectorSettings`: a future provider adapter reads its own key
from the environment at construction time and must not expose it through the
registry, the config endpoint or an error message.

Variables (all optional; blank counts as unset):

``DIRECTOR_DEFAULT_PROVIDER``  provider id used when a request names none
                               (default ``rules-baseline``)
``DIRECTOR_DEFAULT_MODEL``     model for that provider (default: its own default)
``DIRECTOR_TIMEOUT_SECONDS``   per-generation timeout, > 0 and <= 300 (default ``10``)
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from dungeon_director.errors import DirectorConfigError
from dungeon_director.rules import RULES_PROVIDER_ID

__all__ = ["DEFAULT_TIMEOUT_SECONDS", "MAX_TIMEOUT_SECONDS", "DirectorSettings"]

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 300.0


def _read(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True, slots=True)
class DirectorSettings:
    default_provider: str = RULES_PROVIDER_ID
    default_model: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DirectorSettings:
        env = os.environ if environ is None else environ
        timeout = DEFAULT_TIMEOUT_SECONDS
        raw_timeout = _read(env, "DIRECTOR_TIMEOUT_SECONDS")
        if raw_timeout is not None:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                timeout = math.nan
            if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
                raise DirectorConfigError(
                    f"DIRECTOR_TIMEOUT_SECONDS must be a number greater than 0 and at most "
                    f"{MAX_TIMEOUT_SECONDS:g}, got {raw_timeout!r}"
                )
        return cls(
            default_provider=_read(env, "DIRECTOR_DEFAULT_PROVIDER") or RULES_PROVIDER_ID,
            default_model=_read(env, "DIRECTOR_DEFAULT_MODEL"),
            timeout_seconds=timeout,
        )
