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

Shadow evaluation (see ``docs/shadow-mode.md``) is configured by the
``DIRECTOR_SHADOW_*`` variables described on :class:`ShadowSettings`. Unlike the
variables above, a malformed shadow value never stops the director: shadow mode
is optional observation, so a bad value degrades to "that part is off / default"
with a warning that names the variable but never echoes its value.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from dungeon_director.errors import DirectorConfigError
from dungeon_director.providers import MODEL_ID_RE, PROVIDER_ID_RE
from dungeon_director.rules import RULES_PROVIDER_ID

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_SHADOW_TARGETS",
    "MAX_TIMEOUT_SECONDS",
    "DirectorSettings",
    "ShadowSettings",
    "ShadowTarget",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 300.0


def _read(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


#: Hard cap on configured shadow targets: each one is a paid upstream call per request.
MAX_SHADOW_TARGETS = 4
DEFAULT_SHADOW_MAX_IN_FLIGHT = 8
MAX_SHADOW_MAX_IN_FLIGHT = 64
DEFAULT_SHADOW_STORE_SIZE = 128
MAX_SHADOW_STORE_SIZE = 1024
DEFAULT_SHADOW_DRAIN_SECONDS = 5.0
MAX_SHADOW_DRAIN_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ShadowTarget:
    """One provider/model that receives a copy of every request.

    ``model`` ``None`` means the provider's own default model (not
    ``DIRECTOR_DEFAULT_MODEL``, which only applies to the active provider).
    """

    provider: str
    model: str | None = None


@dataclass(frozen=True, slots=True)
class ShadowSettings:
    """Shadow evaluation: extra providers that see the same requests but never answer.

    Variables (all optional; blank counts as unset; shadow mode is off unless
    ``DIRECTOR_SHADOW_TARGETS`` names at least one valid target):

    ``DIRECTOR_SHADOW_TARGETS``          comma-separated ``provider`` or
                                         ``provider:model`` entries, at most
                                         :data:`MAX_SHADOW_TARGETS`
    ``DIRECTOR_SHADOW_TIMEOUT_SECONDS``  per-shadow-call timeout, > 0 and <= 300
                                         (default: ``DIRECTOR_TIMEOUT_SECONDS``)
    ``DIRECTOR_SHADOW_MAX_IN_FLIGHT``    shadow calls allowed at once, 1-64 (default 8);
                                         beyond it new shadow runs are skipped, never queued
    ``DIRECTOR_SHADOW_STORE_SIZE``       comparisons kept in memory, 1-1024 (default 128)
    ``DIRECTOR_SHADOW_DRAIN_SECONDS``    shutdown grace before unfinished shadow calls are
                                         cancelled, 0-60 (default 5)

    ``rejected`` counts configuration entries that were dropped (malformed,
    duplicate or over the cap) so operators can see the degradation without the
    dropped text ever being logged.
    """

    targets: tuple[ShadowTarget, ...] = ()
    timeout_seconds: float | None = None
    max_in_flight: int = DEFAULT_SHADOW_MAX_IN_FLIGHT
    store_size: int = DEFAULT_SHADOW_STORE_SIZE
    drain_seconds: float = DEFAULT_SHADOW_DRAIN_SECONDS
    rejected: int = 0

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ShadowSettings:
        """Parse the environment. Never raises: bad values degrade with a warning."""
        env = os.environ if environ is None else environ
        targets, rejected = _parse_shadow_targets(_read(env, "DIRECTOR_SHADOW_TARGETS"))
        return cls(
            targets=targets,
            timeout_seconds=_lenient_number(
                env,
                "DIRECTOR_SHADOW_TIMEOUT_SECONDS",
                None,
                lambda v: 0 < v <= MAX_TIMEOUT_SECONDS,
                f"a number > 0 and <= {MAX_TIMEOUT_SECONDS:g}",
            ),
            max_in_flight=_lenient_int(
                env,
                "DIRECTOR_SHADOW_MAX_IN_FLIGHT",
                DEFAULT_SHADOW_MAX_IN_FLIGHT,
                1,
                MAX_SHADOW_MAX_IN_FLIGHT,
            ),
            store_size=_lenient_int(
                env,
                "DIRECTOR_SHADOW_STORE_SIZE",
                DEFAULT_SHADOW_STORE_SIZE,
                1,
                MAX_SHADOW_STORE_SIZE,
            ),
            drain_seconds=_lenient_number(
                env,
                "DIRECTOR_SHADOW_DRAIN_SECONDS",
                DEFAULT_SHADOW_DRAIN_SECONDS,
                lambda v: 0 <= v <= MAX_SHADOW_DRAIN_SECONDS,
                f"a number between 0 and {MAX_SHADOW_DRAIN_SECONDS:g}",
            ),
            rejected=rejected,
        )


def _parse_shadow_targets(raw: str | None) -> tuple[tuple[ShadowTarget, ...], int]:
    """Split ``provider[:model],...`` into valid targets plus a rejected count.

    Provider ids cannot contain ``:`` but model ids can, so the *first* colon
    separates them (``groq:openai/gpt-oss-20b``). Rejected entries are only
    counted, never logged: an operator who pasted a credential into this
    variable by mistake must not see it echoed.
    """
    if raw is None:
        return (), 0
    targets: list[ShadowTarget] = []
    rejected = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue  # tolerate stray commas
        provider, sep, model = entry.partition(":")
        provider, model = provider.strip(), model.strip()
        target = ShadowTarget(provider, model or None)
        valid = (
            PROVIDER_ID_RE.match(provider) is not None
            and (not sep or (model != "" and MODEL_ID_RE.match(model) is not None))
            and target not in targets
            and len(targets) < MAX_SHADOW_TARGETS
        )
        if valid:
            targets.append(target)
        else:
            rejected += 1
    if rejected:
        logger.warning(
            "DIRECTOR_SHADOW_TARGETS: %d entr%s rejected (malformed, duplicate or over the "
            "limit of %d); entry text withheld",
            rejected,
            "y" if rejected == 1 else "ies",
            MAX_SHADOW_TARGETS,
        )
    return tuple(targets), rejected


def _lenient_number(
    env: Mapping[str, str],
    name: str,
    default: float | None,
    accept: Callable[[float], bool],
    expected: str,
) -> float | None:
    """A number from the environment, or ``default`` (with a warning) if unusable.

    The warning names the variable and the expectation, never the bad value.
    """
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not accept(value):
        logger.warning("%s must be %s; using the default", name, expected)
        return default
    return value


def _lenient_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        logger.warning("%s must be an integer from %d to %d; using the default", name, low, high)
        return default
    return value


@dataclass(frozen=True, slots=True)
class DirectorSettings:
    default_provider: str = RULES_PROVIDER_ID
    default_model: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    shadow: ShadowSettings = field(default_factory=ShadowSettings)

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
            shadow=ShadowSettings.from_env(env),
        )
