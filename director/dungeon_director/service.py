"""Generation service: selection, timeout, validation and failure conversion.

Every outcome, success or failure, is a canonical :class:`GenerationResponse`
paired with the HTTP status it should travel with. Providers only produce
decisions; this layer owns the policies that must be identical for all of them
so latency and reliability numbers stay comparable:

* one configurable timeout; a timed-out provider call is cancelled, and a
  provider that returns after the deadline (it swallowed the cancellation, or
  blocked the event loop) is reported as a timeout, never as a success;
* if the task running ``generate`` is cancelled (for example the ASGI server
  cancels the request task), the cancellation propagates into the provider call
  and is never swallowed: ``CancelledError`` is a ``BaseException``, so the
  ``except Exception`` clauses below cannot catch it. The director does not
  itself watch for client disconnects;
* providers must be genuinely async: a provider that blocks the event loop
  (``time.sleep``, a synchronous HTTP client) cannot be interrupted here, stalls
  every other request while it runs, and can only be flagged after the fact;
* no retries of any kind: one request is one provider call;
* provider adapters are an untrusted boundary: text an adapter puts in a
  ``ProviderError`` (message, raw excerpt) is never sent to the game or written
  to the log, because it can echo credentials. The game gets a stable message
  per error code; excerpts are only ever the director's own validation output;
* provider results are validated, never trusted: the result object itself,
  and the room even when it is already a ``RoomPlan`` instance (which may have
  been built with ``model_construct`` or mutated after validation). Every
  failure mode maps to a canonical failure envelope, so one
  bad provider can never take the service down;
* a provider that answered with unusable output still spent tokens and time, so
  the failure envelope keeps that result's ``provider_metadata`` and ``usage``.
  Usage is rebuilt field by field in bounds (failure and success alike), since
  an instance may come from ``model_construct`` or be mutated. Provider-*raised*
  errors carry no telemetry: adapter text and state are untrusted;
* unexpected-exception logs carry the provider, model and exception *type*
  only: exception text and tracebacks can echo credentials;
* telemetry is observation only: every telemetry call goes through
  :func:`_observe`, so a failing exporter, span or instrument can never change
  the response, the status code or the cancellation behaviour;
* shadow evaluation (:mod:`dungeon_director.shadow`) is strictly additive: the
  active call is launched exactly as without it, shadow targets run the same
  pipeline in background tasks on their own copy of the request, and nothing a
  shadow does (slow, failing, cancelled, crashing) can change or delay the
  active outcome. Cancelling the active call cancels its shadows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue, ValidationError

from dungeon_director.contracts import (
    ErrorKind,
    GenerationRequest,
    GenerationResponse,
    RoomPlan,
    UsageStats,
)
from dungeon_director.errors import (
    DirectorConfigError,
    ProviderError,
    ProviderSelectionError,
    SelectionReason,
)
from dungeon_director.provider_telemetry import (
    ProviderObservation,
    ProviderOutcome,
    ProviderTelemetry,
    outcome_for_error_code,
)
from dungeon_director.providers import PlanPayload, ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.settings import DirectorSettings
from dungeon_director.shadow import ShadowComparison, ShadowEvaluator, ShadowObserver
from dungeon_director.telemetry import UNKNOWN, DirectorTelemetry, GenerationObservation

__all__ = ["DirectorService", "GenerationOutcome", "status_for_error"]

logger = logging.getLogger(__name__)

_EXCERPT_CHARS = 4096
_SUMMARY_ERRORS = 3

_SELECTION_STATUS = {
    SelectionReason.UNKNOWN_PROVIDER: 404,
    SelectionReason.UNKNOWN_MODEL: 404,
    SelectionReason.PROVIDER_UNAVAILABLE: 503,
}

# Everything not listed is a bad upstream answer: 502.
_ERROR_STATUS = {
    ErrorKind.PROVIDER_TIMEOUT: 504,
    ErrorKind.RATE_LIMITED: 429,
    ErrorKind.BUDGET_EXCEEDED: 429,
    ErrorKind.INTERNAL_ERROR: 500,
    ErrorKind.SCHEMA_VIOLATION: 502,
}


# Public wording for provider-reported failures, keyed by code. Deliberately
# generic: adapter-supplied text is not trusted (see module docstring).
_PUBLIC_MESSAGES = {
    ErrorKind.SCHEMA_VIOLATION: "Provider output did not match the room contract.",
    ErrorKind.INVALID_JSON: "Provider output was not valid JSON.",
    ErrorKind.UNSUPPORTED_CONTRACT_VERSION: "Provider spoke an unsupported contract version.",
    ErrorKind.EMPTY_RESPONSE: "Provider returned an empty response.",
    ErrorKind.PROVIDER_ERROR: "Provider failed to produce a room.",
    ErrorKind.PROVIDER_TIMEOUT: "Provider timed out.",
    ErrorKind.RATE_LIMITED: "Provider rate limit reached.",
    ErrorKind.SAFETY_REFUSAL: "Provider refused the request.",
    ErrorKind.BUDGET_EXCEEDED: "Provider budget exceeded.",
    ErrorKind.INTERNAL_ERROR: "Provider reported an internal error.",
}


def status_for_error(code: ErrorKind) -> int:
    """HTTP status for a canonical failure code (default 502)."""
    return _ERROR_STATUS.get(code, 502)


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """A canonical response plus the HTTP status it travels with.

    ``comparison_id`` is set only when shadow evaluation is enabled: it links
    the active outcome to the shadow records made for the same call. It never
    appears in the response body.
    """

    response: GenerationResponse
    status_code: int
    comparison_id: str | None = None


@dataclass(slots=True)
class _ExecutionObservation:
    observation: GenerationObservation | None
    provider: str = UNKNOWN
    model: str = UNKNOWN
    provider_duration_s: float | None = None
    selection_failed: bool = False


def _observe(step: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Run one telemetry call; whatever it raises is logged (type only) and dropped."""
    try:
        step(*args, **kwargs)
    except Exception as exc:
        logger.warning("telemetry call failed (%s)", type(exc).__name__)


class DirectorService:
    def __init__(
        self,
        registry: ProviderRegistry,
        settings: DirectorSettings,
        telemetry: DirectorTelemetry | None = None,
        *,
        shadow_observers: Sequence[ShadowObserver] = (),
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._telemetry = telemetry if telemetry is not None else DirectorTelemetry(enabled=False)
        # Built from the same TracerProvider director.generate spans use (real,
        # no-op, or broken alike): a #24 provider span is always a proper
        # child, and a broken tracer degrades this exactly like DirectorTelemetry.
        self._provider_telemetry = ProviderTelemetry(self._telemetry.tracer_provider)
        try:
            registry.select(settings.default_provider, settings.default_model)
        except ProviderSelectionError as exc:
            raise DirectorConfigError(
                f"the default provider/model is unusable: {exc.message}"
            ) from exc
        # Shadow targets are optional observation: an unusable one is warned
        # about and skipped per request, never a startup failure.
        self.shadow: ShadowEvaluator | None = None
        if settings.shadow.enabled:
            self.shadow = ShadowEvaluator(
                settings.shadow,
                registry,
                self._run_shadow_target,
                default_timeout=settings.timeout_seconds,
                observers=shadow_observers,
            )
            self.shadow.check_targets()

    async def aclose(self) -> None:
        """Drain or cancel running shadow calls. Safe to call more than once.

        Call this before closing the providers those calls are using.
        """
        if self.shadow is not None:
            await self.shadow.aclose()

    def _begin_provider_observation(
        self,
        parent: GenerationObservation | None,
        provider: str,
        model: str,
        *,
        shadow: bool,
    ) -> ProviderObservation | None:
        """Start the #24 provider-call child span, parented off ``parent``.

        ``parent`` is the request's own ``GenerationObservation`` (may be
        ``None`` if the outer span itself failed to start); its ``.context``
        is the explicit parent context, never ambient/current-span state.
        """
        try:
            return self._provider_telemetry.begin(
                provider=provider,
                model=model,
                execution_mode="shadow" if shadow else "active",
                parent=parent.context if parent is not None else None,
            )
        except Exception as exc:
            logger.warning("provider telemetry start failed (%s)", type(exc).__name__)
            return None

    def _begin_observation(
        self, request: GenerationRequest, is_shadow: bool
    ) -> GenerationObservation | None:
        try:
            return self._telemetry.begin(request, is_shadow=is_shadow)
        except Exception as exc:
            logger.warning("telemetry start failed (%s)", type(exc).__name__)
            return None

    async def generate(
        self,
        request: GenerationRequest,
        *,
        provider: str | None = None,
        model: str | None = None,
        is_shadow: bool = False,
    ) -> GenerationOutcome:
        """Answer one request; only cancellation escapes as an exception.

        ``is_shadow`` is retained as a direct execution hook for callers and
        tests. Configured shadow fanout uses the same path internally.
        """
        provider_id = provider if provider is not None else self._settings.default_provider
        if model is None and provider_id == self._settings.default_provider:
            model = self._settings.default_model

        if is_shadow:
            return await self._execute(
                request,
                provider_id,
                model,
                timeout_seconds=self._settings.timeout_seconds,
                shadow=True,
            )

        comparison: ShadowComparison | None = None

        def start_shadows(selected_provider: str, selected_model: str) -> None:
            nonlocal comparison
            evaluator = self.shadow
            if evaluator is None or not evaluator.enabled:
                return
            try:
                comparison = evaluator.begin(
                    request, provider=selected_provider, model=selected_model
                )
            except Exception as exc:  # shadow machinery must never fail the active call
                logger.error("shadow evaluation could not start: %s", type(exc).__name__)

        try:
            outcome = await self._execute(
                request,
                provider_id,
                model,
                timeout_seconds=self._settings.timeout_seconds,
                on_selected=start_shadows,
            )
        except BaseException as exc:
            # Cancellation (or a bug) ended the active call: its shadows go too.
            if comparison is not None:
                comparison.abort(cancelled=isinstance(exc, asyncio.CancelledError))
            raise
        if comparison is None:
            return outcome
        comparison.finish_active(outcome)
        return replace(outcome, comparison_id=comparison.comparison_id)

    async def _run_shadow_target(
        self, request: GenerationRequest, provider: str, model: str | None, timeout_seconds: float
    ) -> GenerationOutcome:
        """One shadow call: the same pipeline as the active one, on the shadow's own request."""
        return await self._execute(
            request, provider, model, timeout_seconds=timeout_seconds, shadow=True
        )

    async def _execute(
        self,
        request: GenerationRequest,
        provider_id: str,
        model: str | None,
        *,
        timeout_seconds: float,
        on_selected: Callable[[str, str], None] | None = None,
        shadow: bool = False,
    ) -> GenerationOutcome:
        """Observe one execution without letting instrumentation change its outcome."""
        context = _ExecutionObservation(self._begin_observation(request, shadow))
        observation = context.observation
        try:
            outcome = await self._execute_provider(
                request,
                provider_id,
                model,
                timeout_seconds=timeout_seconds,
                on_selected=on_selected,
                shadow=shadow,
                observation=context,
            )
        except asyncio.CancelledError:
            if observation is not None:
                _observe(
                    observation.cancel,
                    provider=context.provider,
                    model=context.model,
                    provider_latency_s=context.provider_duration_s,
                )
            raise
        except Exception:
            if observation is not None:
                _observe(
                    observation.abort,
                    provider=context.provider,
                    model=context.model,
                    provider_latency_s=context.provider_duration_s,
                )
            raise
        else:
            if observation is not None:
                _observe(
                    observation.finish,
                    outcome.response,
                    http_status=outcome.status_code,
                    provider=context.provider,
                    model=context.model,
                    provider_latency_s=context.provider_duration_s,
                    selection_failed=context.selection_failed,
                )
            return outcome
        finally:
            if observation is not None:
                _observe(observation.end)

    async def _execute_provider(
        self,
        request: GenerationRequest,
        provider_id: str,
        model: str | None,
        *,
        timeout_seconds: float,
        on_selected: Callable[[str, str], None] | None,
        shadow: bool,
        observation: _ExecutionObservation,
    ) -> GenerationOutcome:
        """Select, call, time-limit and validate one provider; failures become envelopes.

        ``on_selected`` runs once the provider/model resolved and before the
        provider is awaited (the shadow launch point). ``shadow`` only changes
        the log prefix.
        """
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        tag = "shadow " if shadow else ""

        # Set once a provider/model is selected (below); stays None for a
        # selection failure, so fail() never creates a span for a call that
        # never happened.
        provider_observation: ProviderObservation | None = None

        def fail(
            code: ErrorKind,
            message: str,
            *,
            status: int | None = None,
            raw_excerpt: str | None = None,
            provider_metadata: dict[str, JsonValue] | None = None,
            resolved_model: str | None = None,
            usage: UsageStats | None = None,
        ) -> GenerationOutcome:
            response = GenerationResponse.failure(
                request_id=request.request_id,
                run_id=request.run_id,
                provider=provider_id,
                model=resolved_model or model,
                code=code,
                message=message,
                raw_excerpt=raw_excerpt,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                usage=usage,
                provider_metadata=provider_metadata,
            )
            response.metadata.latency_ms = _elapsed_ms(started)
            if provider_observation is not None:
                timeout_origin = (
                    provider_metadata.get("timeout_origin")
                    if isinstance(provider_metadata, dict)
                    else None
                )
                _observe(
                    provider_observation.complete,
                    outcome_for_error_code(code),
                    error_code=code.value,
                    timeout_origin=timeout_origin if isinstance(timeout_origin, str) else None,
                    call_duration_s=observation.provider_duration_s,
                    usage=usage,
                    provider_metadata=provider_metadata,
                )
            return GenerationOutcome(response, status or status_for_error(code))

        try:
            selection = self._registry.select(provider_id, model)
        except ProviderSelectionError as exc:
            observation.selection_failed = True
            if exc.reason is not SelectionReason.UNKNOWN_PROVIDER:
                observation.provider = provider_id
            return fail(
                ErrorKind.PROVIDER_ERROR,
                exc.message,
                status=_SELECTION_STATUS[exc.reason],
                provider_metadata={"selection_error": exc.reason.value},
            )
        model = selection.model
        observation.provider = provider_id
        observation.model = selection.model
        if on_selected is not None:
            on_selected(provider_id, model)

        provider_observation = self._begin_provider_observation(
            observation.observation, provider_id, model, shadow=shadow
        )

        deadline = asyncio.timeout(timeout_seconds)
        call_started = time.perf_counter()

        def deadline_missed() -> GenerationOutcome:
            logger.warning(
                "%sprovider %s/%s missed the %gs deadline", tag, provider_id, model, timeout_seconds
            )
            return fail(
                ErrorKind.PROVIDER_TIMEOUT,
                f"Provider did not respond within {timeout_seconds:g} seconds.",
                provider_metadata={"timeout_origin": "director_deadline"},
            )

        try:
            try:
                try:
                    async with deadline:
                        result = await selection.provider.generate(request, model=selection.model)
                finally:
                    observation.provider_duration_s = time.perf_counter() - call_started
            except asyncio.CancelledError:
                # The provider span ends here too: cancellation never reaches
                # the fail()/success paths below.
                if provider_observation is not None:
                    _observe(
                        provider_observation.cancel,
                        call_duration_s=observation.provider_duration_s,
                    )
                raise
            except TimeoutError:
                if deadline.expired():
                    return deadline_missed()
                # The provider raised TimeoutError itself (its own upstream timed out).
                logger.warning("%sprovider %s/%s raised TimeoutError", tag, provider_id, model)
                return fail(
                    ErrorKind.PROVIDER_TIMEOUT,
                    "Provider reported a timeout from its own upstream.",
                    provider_metadata={"timeout_origin": "provider"},
                )
            except ProviderError as exc:
                # Log the code only: message and excerpt are adapter-controlled and
                # may contain credentials.
                code = exc.code if isinstance(exc.code, ErrorKind) else ErrorKind.PROVIDER_ERROR
                logger.warning(
                    "%sprovider %s/%s reported %s (adapter text withheld)",
                    tag,
                    provider_id,
                    model,
                    code.value,
                )
                return fail(
                    code,
                    _PUBLIC_MESSAGES[code],
                    provider_metadata=(
                        {"timeout_origin": "provider"} if code is ErrorKind.PROVIDER_TIMEOUT else None
                    ),
                )
            except Exception as exc:
                # Exception text and tracebacks can carry URLs or tokens: neither the
                # log nor the game gets them, only the exception type.
                logger.error(
                    "%sprovider %s/%s raised unexpected %s",
                    tag,
                    provider_id,
                    model,
                    type(exc).__name__,
                )
                return fail(ErrorKind.PROVIDER_ERROR, f"Provider raised {type(exc).__name__}.")

            # A provider that swallowed the cancellation, or blocked the event loop,
            # can come back after the deadline: the answer is stale, not a success.
            if deadline.expired() or time.perf_counter() - call_started >= timeout_seconds:
                return deadline_missed()

            # A provider that answered but produced unusable output still spent
            # tokens and time: keep that telemetry on the failure so benchmarks can
            # count it. The result is untrusted, so usage is rebuilt in bounds and
            # metadata must at least be a dict (the envelope re-checks its budget).
            usage = _canonical_usage(result.usage) if isinstance(result, ProviderResult) else None
            telemetry: dict[str, UsageStats | dict[str, JsonValue] | None] = {"usage": usage}
            if isinstance(result, ProviderResult) and isinstance(result.provider_metadata, dict):
                telemetry["provider_metadata"] = result.provider_metadata

            try:
                if not isinstance(result, ProviderResult):
                    raise _InvalidOutput(
                        ErrorKind.SCHEMA_VIOLATION, "Provider returned an invalid result object."
                    )
                room = _coerce_room(result.payload)
                response = GenerationResponse.success_from_request(
                    request,
                    room=room,
                    provider=provider_id,
                    model=model,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    usage=usage,
                    provider_metadata=result.provider_metadata,
                )
            except _InvalidOutput as exc:
                logger.warning(
                    "%sprovider %s/%s output rejected: %s", tag, provider_id, model, exc.code.value
                )
                return fail(
                    exc.code,
                    exc.message,
                    raw_excerpt=exc.raw_excerpt,
                    **telemetry,
                )
            except ValidationError as exc:
                return fail(
                    ErrorKind.SCHEMA_VIOLATION,
                    _summarize(exc),
                    **telemetry,
                )
            except ValueError as exc:  # e.g. room depth differs from the request depth
                return fail(
                    ErrorKind.SCHEMA_VIOLATION,
                    str(exc),
                    **telemetry,
                )
            except Exception as exc:
                logger.error(
                    "%sprovider %s/%s result could not be processed: %s",
                    tag,
                    provider_id,
                    model,
                    type(exc).__name__,
                )
                return fail(
                    ErrorKind.INTERNAL_ERROR,
                    "Director failed to process the provider result.",
                    **telemetry,
                )

            response.metadata.latency_ms = _elapsed_ms(started)
            if provider_observation is not None:
                _observe(
                    provider_observation.complete,
                    ProviderOutcome.SUCCESS,
                    call_duration_s=observation.provider_duration_s,
                    usage=usage,
                    provider_metadata=result.provider_metadata,
                )
            return GenerationOutcome(response, 200)
        finally:
            # Safety net: whatever path was taken above already completed the
            # span (fail()/the success branch/the cancellation branch), so
            # this is a no-op; it only matters if some future edit adds a
            # path that forgets to.
            if provider_observation is not None:
                _observe(provider_observation.end)


class _InvalidOutput(Exception):
    """The director's own verdict on unusable provider output.

    Unlike :class:`ProviderError` (adapter-controlled text), the message and
    excerpt here are produced by this module, so they are safe to return.
    """

    def __init__(self, code: ErrorKind, message: str, raw_excerpt: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw_excerpt = raw_excerpt


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _canonical_usage(usage: object) -> UsageStats | None:
    """A fresh, in-bounds :class:`UsageStats` built from an untrusted one, or ``None``.

    An ``isinstance`` check proves nothing about the bounds: instances can come
    from ``model_construct`` or be mutated after validation, and Pydantic does
    not revalidate instances when they are nested into the envelope. Each field
    is therefore re-validated on its own (strictly, so ``True`` or ``"12"`` are
    not coerced to tokens), invalid fields are dropped, and a non-finite cost is
    dropped too because it cannot be serialized. The input is never mutated.
    """
    if not isinstance(usage, UsageStats):
        return None
    try:
        raw = usage.model_dump(mode="python", warnings=False)
    except Exception:  # hostile subclasses / half-built instances
        return None
    clean: dict[str, int | float] = {}
    for name in ("input_tokens", "output_tokens", "estimated_cost_usd"):
        value = raw.get(name)
        if value is None:
            continue
        try:
            checked = getattr(UsageStats.model_validate({name: value}, strict=True), name)
        except (AttributeError, ValidationError):
            continue
        if isinstance(checked, float) and not math.isfinite(checked):
            continue
        clean[name] = checked
    return UsageStats(**clean) if clean else None


def _coerce_room(payload: PlanPayload | None) -> RoomPlan:
    """Turn whatever a provider returned into a validated :class:`RoomPlan`."""
    if isinstance(payload, RoomPlan):
        # An instance may have been built with model_construct() or mutated
        # after validation, so validate its data again from scratch (nested
        # models included).
        try:
            payload = payload.model_dump(mode="python", warnings=False)
        except Exception as exc:
            raise _InvalidOutput(
                ErrorKind.SCHEMA_VIOLATION, "Provider returned a RoomPlan that could not be read."
            ) from exc
    if not payload:
        raise _InvalidOutput(ErrorKind.EMPTY_RESPONSE, "Provider returned an empty response.")
    excerpt = _excerpt(payload)
    try:
        if isinstance(payload, str | bytes):
            if not payload.strip():
                raise _InvalidOutput(
                    ErrorKind.EMPTY_RESPONSE, "Provider returned an empty response."
                )
            return RoomPlan.model_validate_json(payload)
        return RoomPlan.model_validate(payload)
    except ValidationError as exc:
        is_json_error = any(error["type"] == "json_invalid" for error in exc.errors())
        raise _InvalidOutput(
            ErrorKind.INVALID_JSON if is_json_error else ErrorKind.SCHEMA_VIOLATION,
            "Provider output is not valid JSON."
            if is_json_error
            else f"Provider output failed RoomPlan validation: {_summarize(exc)}",
            raw_excerpt=excerpt,
        ) from exc


def _summarize(exc: ValidationError) -> str:
    """Field paths and messages only: never echo the offending input values."""
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    parts = [f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}" for e in errors]
    more = len(parts) - _SUMMARY_ERRORS
    text = "; ".join(parts[:_SUMMARY_ERRORS])
    return f"{text}; and {more} more" if more > 0 else text


def _excerpt(payload: object) -> str:
    """Best-effort bounded text of a malformed payload; must never raise."""
    try:
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            text = payload
        else:
            text = json.dumps(payload, default=repr, ensure_ascii=False)
    except Exception:  # non-string keys, circular references, hostile __repr__
        try:
            text = repr(payload)
        except Exception:
            text = f"<unrepresentable {type(payload).__name__}>"
    return text[:_EXCERPT_CHARS]
