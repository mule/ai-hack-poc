"""OpenTelemetry GenAI spans for individual provider/adapter invocations (#24).

Every provider call the service makes (Groq, Cerebras, TypeSafe Jev,
Cloudflare Jev, or the rules baseline) gets exactly one
``director.provider.invoke`` child span, parented explicitly under that
request's ``director.generate`` span (see
:attr:`dungeon_director.telemetry.GenerationObservation.context`) — never
through ambient/"current span" propagation, so concurrent shadow tasks can
never leak a parent into each other's spans.

Design rules, mirroring ``telemetry.py`` (#11/#22) exactly:

* **Telemetry never changes generation.** Every OpenTelemetry call is guarded
  the same way as ``telemetry.py``: an exception is logged by *type* only and
  swallowed. ``ProviderTelemetry`` is built directly from the same
  ``TracerProvider`` the request's ``DirectorTelemetry`` already uses (which
  is a real provider, a no-op one, or a broken one that raises); a broken
  ``get_tracer``/``start_span``/``set_attributes``/``set_status``/``end`` call
  degrades this module alone, the same way it degrades ``telemetry.py``.
* **The span's duration is the adapter call, not game waiting.** The span
  starts right before the provider is awaited and is only extended long
  enough to learn whether the response normalized (a few microseconds of
  local ``RoomPlan`` validation) so schema failures can be distinguished on
  the span itself. The *exact* network/adapter latency is always also
  recorded as the explicit ``director.provider.call_duration_ms`` attribute
  (the same number the parent span already carries as
  ``director.provider_latency_ms``), so a slightly longer wall-clock span
  never hides the real number.
* **Nothing sensitive is recorded.** No raw provider payloads, prompts,
  headers, credentials, or exception text — only the machine-readable error
  *code*. Provider-reported metadata is mirrored onto the span only through
  ``_METADATA_ALLOWLIST`` (bounded Jev confidence/score scalars, provider
  response/request identifiers, finish reasons, timing breakdowns): anything
  not in that list, or not a small bounded scalar, is dropped silently.
* **Standard GenAI fields plus usage**, same naming as the parent span:
  ``gen_ai.system``, ``gen_ai.request.model``, ``gen_ai.response.model``
  (the model the provider actually reports, when it reports one; otherwise
  the requested model) and ``gen_ai.usage.*``.
* **No span for a call that never happened.** Selection failures (unknown
  provider/model, unavailable provider) never reach a provider call, so no
  provider span is created for them — only ``director.generate`` records
  that outcome, exactly as before #24.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from opentelemetry.trace import INVALID_SPAN, NoOpTracerProvider, Span, SpanKind, Status, StatusCode

from dungeon_director.contracts import ErrorKind, UsageStats
from dungeon_director.telemetry import NONE, UNKNOWN, _never_raises
from dungeon_director.telemetry_schema import _SECRET_LIKE_RE

__all__ = [
    "PROVIDER_SPAN_NAME",
    "ProviderObservation",
    "ProviderOutcome",
    "ProviderTelemetry",
]

logger = logging.getLogger(__name__)

TRACER_NAME = "dungeon-director.provider"
PROVIDER_SPAN_NAME = "director.provider.invoke"

# Same shape telemetry.py/telemetry_schema.py enforce for labels and provider/
# model ids: no spaces, no colons-with-following-value shapes that could hide
# a header, bounded length. Adapter-reported string metadata (finish_reason,
# cerebras_id/model, jev_model, ...) is only *length*-truncated by the
# adapters themselves — never content-validated — so this module re-checks
# shape and, on top of that, rejects anything that merely looks credential-
# shaped (``_SECRET_LIKE_RE``, the same check telemetry_schema.py uses for the
# same reason), rather than trusting the adapter's truncation alone.
_LABEL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}$")
# cerebras.py's ``time_info`` fields: bounded dynamic numeric keys.
_META_TIME_KEY_RE = re.compile(r"^time_[A-Za-z0-9_]{1,32}$")


def _safe_meta_string(value: object) -> str | None:
    """A bounded, non-credential-shaped metadata string, or ``None``.

    Two independent checks, neither trusting the field name: ``_LABEL_RE``
    rejects spaces and most punctuation (so ``"Bearer sk-..."`` fails on the
    space alone), and ``_SECRET_LIKE_RE`` additionally rejects a *spaceless*
    token that still looks like a credential (``sk-...``, ``api_key...``).
    """
    if not isinstance(value, str) or not _LABEL_RE.match(value):
        return None
    if _SECRET_LIKE_RE.search(value):
        return None
    return value


_TIMEOUT_ORIGINS = frozenset({"director_deadline", "provider"})

# Mirrors telemetry.py's Outcome->schema-error mapping, kept local so this
# module stays independent (no coupling beyond the shared guard helper).
_SCHEMA_CODES = frozenset(
    {
        ErrorKind.SCHEMA_VIOLATION,
        ErrorKind.INVALID_JSON,
        ErrorKind.UNSUPPORTED_CONTRACT_VERSION,
        ErrorKind.EMPTY_RESPONSE,
    }
)

#: The complete set of provider-reported metadata keys ever mirrored onto a
#: provider span. Explicit and closed, like telemetry.py's own
#: ``METRIC_DIMENSIONS``/``_TIMEOUT_ORIGINS``: a new adapter field needs a
#: reviewed addition here before it can reach a span, and nothing dynamic
#: (raw payload/prompt/exception text) is ever eligible regardless.
_METADATA_ALLOWLIST = frozenset(
    {
        # shared adapter fields
        "upstream_http_status",
        "adapter_elapsed_ms",
        # Jev (typesafe-jev / cloudflare-jev): bounded confidence/score summaries
        "jev_model",
        "room_type_confidence",
        "room_type_provider_choice",
        "size_confidence",
        "size_provider_choice",
        "danger_score",
        "danger_confidence",
        "enemy_density_score",
        "enemy_density_confidence",
        "loot_density_score",
        "loot_density_confidence",
        "has_secret_probability",
        "secret_allowed",
        "exit_count",
        "exit_count_confidence",
        "atmosphere_choice",
        "atmosphere_confidence",
        # cerebras
        "cerebras_id",
        "cerebras_model",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
        # groq
        "groq_model",
        "groq_request_id",
        "service_tier",
        "system_fingerprint",
        "response_chars",
        "total_tokens",
        "reasoning_tokens",
        "queue_time_s",
        "prompt_time_s",
        "completion_time_s",
        "total_time_s",
    }
)
_METADATA_MAX_ATTRIBUTES = 32
_STRING_METADATA_KEYS = frozenset(
    {
        "jev_model",
        "room_type_provider_choice",
        "size_provider_choice",
        "atmosphere_choice",
        "cerebras_id",
        "cerebras_model",
        "finish_reason",
        "groq_model",
        "groq_request_id",
        "service_tier",
        "system_fingerprint",
    }
)

#: Fields that carry the provider's own reported model id, checked in order.
_RESPONSE_MODEL_KEYS = ("jev_model", "cerebras_model", "groq_model")


class ProviderOutcome(StrEnum):
    """The provider-call span's ``director.provider.outcome``: a closed set."""

    SUCCESS = "success"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    SCHEMA_ERROR = "schema_error"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


def outcome_for_error_code(code: ErrorKind) -> ProviderOutcome:
    """Map a canonical :class:`ErrorKind` onto the bounded provider outcome.

    ``INTERNAL_ERROR`` is deliberately not folded into ``PROVIDER_ERROR``: it
    means the *director's own* processing of an otherwise-usable provider
    result failed (e.g. building the response envelope), not that the
    provider misbehaved — the span should not blame the provider for it.
    """
    if code is ErrorKind.PROVIDER_TIMEOUT:
        return ProviderOutcome.TIMEOUT
    if code is ErrorKind.INTERNAL_ERROR:
        return ProviderOutcome.INTERNAL_ERROR
    if code in _SCHEMA_CODES:
        return ProviderOutcome.SCHEMA_ERROR
    return ProviderOutcome.PROVIDER_ERROR


def _label(value: object) -> str:
    return value if isinstance(value, str) and _LABEL_RE.match(value) else UNKNOWN


def _bounded_metadata_attributes(metadata: object) -> dict[str, Any]:
    """Allowlisted, bounded, scalar-only provider metadata for span attributes.

    ``metadata`` is adapter-controlled and therefore untrusted (see
    ``service.py``'s module docstring): only keys in ``_METADATA_ALLOWLIST``
    (or cerebras's bounded ``time_*`` fields) are ever considered, and only
    when the value is itself a small finite scalar. Dicts, lists, ``None``
    and anything else (in particular the Jev ``*_probabilities`` maps, which
    are not scalars) are dropped silently — never raised on, never partially
    echoed.
    """
    if not isinstance(metadata, Mapping):
        return {}
    attributes: dict[str, Any] = {}
    for key, value in metadata.items():
        if len(attributes) >= _METADATA_MAX_ATTRIBUTES:
            break
        if not isinstance(key, str):
            continue
        if key not in _METADATA_ALLOWLIST and not _META_TIME_KEY_RE.match(key):
            continue
        attr = f"director.provider.meta.{key}"
        if key in _STRING_METADATA_KEYS:
            if (safe := _safe_meta_string(value)) is not None:
                attributes[attr] = safe
        elif key == "secret_allowed":
            if isinstance(value, bool):
                attributes[attr] = value
        elif isinstance(value, int | float) and not isinstance(value, bool):
            maximum = (
                1.0
                if key.endswith("_confidence") or key == "has_secret_probability"
                else 1_000_000_000.0
            )
            if math.isfinite(value) and 0 <= value <= maximum:
                attributes[attr] = round(value, 6)
    return attributes


def _response_model(metadata: object, requested_model: str) -> str:
    """The provider's own reported model id when present, else the requested one."""
    if isinstance(metadata, Mapping):
        for key in _RESPONSE_MODEL_KEYS:
            if (safe := _safe_meta_string(metadata.get(key))) is not None:
                return safe
    return requested_model


def _usage_attributes(usage: object) -> dict[str, Any]:
    """Bounded ``gen_ai.usage.*`` attributes from an untrusted ``UsageStats``.

    ``usage`` may come from ``model_construct`` or be mutated after
    validation (the same concern ``service.py``'s ``_canonical_usage``
    guards against), so every field is re-checked by type and range rather
    than trusted because ``isinstance(usage, UsageStats)`` is true.
    """
    if not isinstance(usage, UsageStats):
        return {}
    attributes: dict[str, Any] = {}
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    if (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and 0 <= input_tokens <= 10_000_000
    ):
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if (
        isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and 0 <= output_tokens <= 10_000_000
    ):
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    if "gen_ai.usage.input_tokens" in attributes and "gen_ai.usage.output_tokens" in attributes:
        attributes["gen_ai.usage.total_tokens"] = (
            attributes["gen_ai.usage.input_tokens"] + attributes["gen_ai.usage.output_tokens"]
        )
    cost = usage.estimated_cost_usd
    if (
        isinstance(cost, int | float)
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
    ):
        attributes["gen_ai.usage.cost"] = float(cost)
    return attributes


class ProviderTelemetry:
    """Owns the tracer for provider-call spans.

    Built directly from the request's :class:`~dungeon_director.telemetry.
    DirectorTelemetry` tracer provider (real, no-op, or broken — this class
    degrades exactly like ``DirectorTelemetry`` does for each). Every public
    method is safe to call from the request path: none of them raise.
    """

    def __init__(self, tracer_provider: Any) -> None:
        try:
            self._tracer = tracer_provider.get_tracer(TRACER_NAME)
        except Exception as exc:
            logger.warning(
                "provider telemetry tracer creation failed (%s); provider spans disabled",
                type(exc).__name__,
            )
            self._tracer = NoOpTracerProvider().get_tracer(TRACER_NAME)

    def begin(
        self,
        *,
        provider: str,
        model: str,
        execution_mode: str,
        parent: Any = None,
        correlation: Mapping[str, str] | None = None,
    ) -> ProviderObservation:
        """Start the ``director.provider.invoke`` span for one adapter call.

        ``parent`` is the explicit :class:`opentelemetry.context.Context`
        carrying the ``director.generate`` span (see
        ``GenerationObservation.context``) — never the ambient/current
        context, so this cannot pick up an unrelated concurrent task's span.
        """
        label_provider = _label(provider)
        label_model = _label(model)
        span = self._start_span(
            {
                **(correlation or {}),
                "gen_ai.system": label_provider,
                "gen_ai.provider.name": label_provider,
                "gen_ai.operation.name": "chat"
                if label_provider != "rules-baseline"
                else "generate_room",
                "gen_ai.request.model": label_model,
                "director.provider.execution_mode": execution_mode,
            },
            parent,
        )
        return ProviderObservation(self, span, provider=label_provider, model=label_model)

    # -- guarded primitives --------------------------------------------------

    def _start_span(self, attributes: Mapping[str, Any], parent: Any) -> Span:
        try:
            return self._tracer.start_span(
                PROVIDER_SPAN_NAME, context=parent, kind=SpanKind.CLIENT, attributes=attributes
            )
        except Exception as exc:
            logger.warning("provider span start failed (%s)", type(exc).__name__)
            return INVALID_SPAN

    @_never_raises
    def _set_attributes(self, span: Span, attributes: Mapping[str, Any]) -> None:
        span.set_attributes(attributes)

    @_never_raises
    def _set_status(self, span: Span, outcome: ProviderOutcome, error_code: str) -> None:
        if outcome is ProviderOutcome.SUCCESS:
            span.set_status(Status(StatusCode.OK))
        elif outcome is not ProviderOutcome.CANCELLED:
            span.set_status(Status(StatusCode.ERROR, description=error_code))

    @_never_raises
    def _end_span(self, span: Span) -> None:
        span.end()


class ProviderObservation:
    """One in-flight provider call. Exactly one terminal call ends the span.

    ``complete`` (and its ``cancel``/``abort`` shorthands) is terminal and
    idempotent — the first call wins; ``end`` is the safety net that closes a
    span no terminal call reached. None of them raise.
    """

    def __init__(
        self, telemetry: ProviderTelemetry, span: Span, *, provider: str, model: str
    ) -> None:
        self._telemetry = telemetry
        self._span = span
        self._provider = provider
        self._model = model
        self._done = False

    def complete(
        self,
        outcome: ProviderOutcome,
        *,
        error_code: str = NONE,
        timeout_origin: str | None = None,
        call_duration_s: float | None = None,
        usage: UsageStats | None = None,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Record the provider call's outcome; safe to call more than once.

        ``gen_ai.response.model`` is always derived from ``provider_metadata``
        here (via ``_response_model``), never taken from the caller: the
        requested/served model can differ (a provider may route to a pinned
        deployment version), and deriving it in one place means every call
        site gets that distinction for free instead of having to remember to
        look it up.
        """
        if self._done:
            return
        self._done = True
        try:
            attributes: dict[str, Any] = {
                "director.provider.outcome": outcome.value,
                "gen_ai.system": self._provider,
                "gen_ai.request.model": self._model,
                "gen_ai.response.model": _response_model(provider_metadata, self._model),
            }
            if outcome is ProviderOutcome.SUCCESS:
                attributes["director.provider.schema_valid"] = True
            elif outcome is ProviderOutcome.SCHEMA_ERROR:
                attributes["director.provider.schema_valid"] = False
            if outcome is not ProviderOutcome.SUCCESS and error_code != NONE:
                attributes["director.provider.error_code"] = error_code
            if timeout_origin in _TIMEOUT_ORIGINS:
                attributes["director.provider.timeout_origin"] = timeout_origin
            if call_duration_s is not None:
                attributes["director.provider.call_duration_ms"] = call_duration_s * 1000.0
            attributes.update(_usage_attributes(usage))
            attributes.update(_bounded_metadata_attributes(provider_metadata))

            telemetry = self._telemetry
            telemetry._set_attributes(self._span, attributes)
            telemetry._set_status(self._span, outcome, error_code)
        finally:
            self._telemetry._end_span(self._span)

    def cancel(self, *, call_duration_s: float | None = None) -> None:
        """The request task was cancelled while this provider call was in flight."""
        self.complete(ProviderOutcome.CANCELLED, call_duration_s=call_duration_s)

    def abort(self, *, call_duration_s: float | None = None) -> None:
        """An unexpected exception is escaping the director around this call."""
        self.complete(
            ProviderOutcome.INTERNAL_ERROR,
            error_code=ErrorKind.INTERNAL_ERROR.value,
            call_duration_s=call_duration_s,
        )

    def end(self) -> None:
        """Close the span if no terminal call did."""
        if not self._done:
            self._done = True
            self._telemetry._end_span(self._span)
