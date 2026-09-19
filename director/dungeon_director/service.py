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
  only: exception text and tracebacks can echo credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime

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
from dungeon_director.providers import PlanPayload, ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.settings import DirectorSettings

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
    response: GenerationResponse
    status_code: int


class DirectorService:
    def __init__(self, registry: ProviderRegistry, settings: DirectorSettings) -> None:
        self._registry = registry
        self._settings = settings
        try:
            registry.select(settings.default_provider, settings.default_model)
        except ProviderSelectionError as exc:
            raise DirectorConfigError(
                f"the default provider/model is unusable: {exc.message}"
            ) from exc

    async def generate(
        self,
        request: GenerationRequest,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> GenerationOutcome:
        """Answer one request; only cancellation escapes as an exception."""
        started_at = datetime.now(UTC)
        started = time.perf_counter()

        provider_id = provider if provider is not None else self._settings.default_provider
        if model is None and provider_id == self._settings.default_provider:
            model = self._settings.default_model

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
            return GenerationOutcome(response, status or status_for_error(code))

        try:
            selection = self._registry.select(provider_id, model)
        except ProviderSelectionError as exc:
            return fail(
                ErrorKind.PROVIDER_ERROR,
                exc.message,
                status=_SELECTION_STATUS[exc.reason],
                provider_metadata={"selection_error": exc.reason.value},
            )
        model = selection.model

        timeout_seconds = self._settings.timeout_seconds
        deadline = asyncio.timeout(timeout_seconds)
        call_started = time.perf_counter()

        def deadline_missed() -> GenerationOutcome:
            logger.warning(
                "provider %s/%s missed the %gs deadline", provider_id, model, timeout_seconds
            )
            return fail(
                ErrorKind.PROVIDER_TIMEOUT,
                f"Provider did not respond within {timeout_seconds:g} seconds.",
                provider_metadata={"timeout_origin": "director_deadline"},
            )

        try:
            async with deadline:
                result = await selection.provider.generate(request, model=selection.model)
        except TimeoutError:
            if deadline.expired():
                return deadline_missed()
            # The provider raised TimeoutError itself (its own upstream timed out).
            logger.warning("provider %s/%s raised TimeoutError", provider_id, model)
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
                "provider %s/%s reported %s (adapter text withheld)",
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
                "provider %s/%s raised unexpected %s", provider_id, model, type(exc).__name__
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
            logger.warning("provider %s/%s output rejected: %s", provider_id, model, exc.code.value)
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
                "provider %s/%s result could not be processed: %s",
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
        return GenerationOutcome(response, 200)


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
