"""Shadow evaluation: run extra providers on the same request without letting them answer.

One *active* provider controls the canonical response. Zero or more configured
*shadow* targets receive an identical, independent copy of the validated
:class:`GenerationRequest` at the same moment, run under the same service
policies (timeout, validation, failure conversion), and have their raw
canonical outcome recorded for later comparison. Nothing a shadow does can
reach the game:

* shadows run as separate background tasks, so a slow shadow never delays the
  active answer, and the active path only pays for a synchronous, allocation-
  light launch step;
* every shadow gets a deep copy of the request made *before* the active call
  starts, so a shadow that mutates its input cannot change what the active
  provider (or a later shadow) sees;
* launching, recording and observer failures are all swallowed and logged by
  exception *type* only (adapter and config text can echo credentials);
* if the active request is cancelled (client disconnect, server cancel) its
  shadows are cancelled with it, and the cancellation is never delayed or
  swallowed here;
* the number of concurrent shadow calls, stored comparisons and records per
  comparison are all bounded; overflow is skipped or evicted, never queued.

Records and metrics
-------------------
Each execution, active or shadow, produces exactly one immutable
:class:`ExecutionRecord` carrying the raw :class:`GenerationOutcome` (the
canonical response with its timing, error, usage and provider metadata, plus
the HTTP status it would have travelled with), the provider/model, the request
and run ids, and the ``comparison_id`` that links the executions of one
generate call. Records go to every :class:`ShadowObserver`; the built-in
:class:`ShadowStore` keeps the most recent comparisons in memory.

``comparison_id``, ``request_id`` and ``run_id`` are unbounded: they belong on
records, logs and trace attributes, **never on metric labels**.
:meth:`ExecutionRecord.metric_labels` is the label-safe projection: every value
comes from a small closed set (role, status, reason) or from the provider
registry.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from dungeon_director.contracts import GenerationRequest
from dungeon_director.errors import ProviderSelectionError
from dungeon_director.registry import ProviderRegistry
from dungeon_director.settings import ShadowSettings, ShadowTarget

if TYPE_CHECKING:  # service.py imports this module; only the type is needed here
    from dungeon_director.service import GenerationOutcome

__all__ = [
    "METRIC_LABEL_NAMES",
    "ComparisonMeta",
    "ComparisonSnapshot",
    "ExecutionRecord",
    "ExecutionRole",
    "ExecutionStatus",
    "ShadowComparison",
    "ShadowEvaluator",
    "ShadowObserver",
    "ShadowStore",
    "SkipReason",
]

logger = logging.getLogger(__name__)

#: How long ``aclose`` waits for cancelled shadow tasks to actually finish.
_CANCEL_GRACE_SECONDS = 1.0
#: Label used instead of an id the registry does not know (requests can name anything).
_OTHER = "other"
_NONE = "none"

#: The only label names :meth:`ExecutionRecord.metric_labels` will ever return.
METRIC_LABEL_NAMES = ("role", "provider", "model", "status", "reason")

#: Runs one provider/model under the service policies: ``(request, provider, model, timeout)``.
ShadowRunner = Callable[
    [GenerationRequest, str, "str | None", float], "Awaitable[GenerationOutcome]"
]


class ExecutionRole(StrEnum):
    ACTIVE = "active"
    SHADOW = "shadow"


class ExecutionStatus(StrEnum):
    SUCCESS = "success"  # a canonical success response
    FAILURE = "failure"  # a canonical failure response (timeout, bad output, ...)
    CANCELLED = "cancelled"  # cancelled before finishing; no outcome
    SKIPPED = "skipped"  # never started; see :class:`SkipReason`
    ERROR = "error"  # the director itself failed around the call; no outcome


class SkipReason(StrEnum):
    """Why a configured shadow target did not run for one request."""

    UNKNOWN_PROVIDER = "unknown_provider"
    UNKNOWN_MODEL = "unknown_model"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    OVERLOADED = "overloaded"
    SHUTTING_DOWN = "shutting_down"
    LAUNCH_FAILED = "launch_failed"


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    """The immutable result of one active or shadow execution.

    ``outcome`` is the raw canonical outcome exactly as the service produced it
    (``None`` when nothing was produced: cancelled, skipped or errored).
    ``duration_ms`` is wall-clock time from launch to the moment the record was
    made, measured the same way for active and shadow so the numbers compare.
    """

    comparison_id: str
    role: ExecutionRole
    provider: str
    model: str | None
    registered: bool
    status: ExecutionStatus
    request_id: str
    run_id: str
    started_at: datetime
    completed_at: datetime
    duration_ms: float
    outcome: GenerationOutcome | None = None
    skip_reason: SkipReason | None = None

    def metric_labels(self) -> dict[str, str]:
        """Bounded, low-cardinality labels for this execution.

        Never contains ``comparison_id``, ``request_id`` or ``run_id``. The
        provider and model appear only when the registry vouches for them;
        anything else (a typo in a query string, a stale config entry) is
        reported as ``other`` so callers cannot mint new label values.
        """
        reason = _NONE
        if self.skip_reason is not None:
            reason = self.skip_reason.value
        elif self.outcome is not None and self.outcome.response.metadata.error is not None:
            reason = self.outcome.response.metadata.error.code.value
        return {
            "role": self.role.value,
            "provider": self.provider if self.registered else _OTHER,
            "model": (self.model or _NONE) if self.registered else _OTHER,
            "status": self.status.value,
            "reason": reason,
        }


@dataclass(frozen=True, slots=True)
class ComparisonMeta:
    """Identity of one comparison: the executions that share a generate call."""

    comparison_id: str
    request_id: str
    run_id: str
    created_at: datetime
    #: The active execution plus one per configured shadow target (including skipped ones).
    expected: int
    #: Explicit parent span context; never exported as an attribute or persisted.
    parent_context: Any = None


@dataclass(frozen=True, slots=True)
class ComparisonSnapshot:
    meta: ComparisonMeta
    records: tuple[ExecutionRecord, ...]

    @property
    def complete(self) -> bool:
        return len(self.records) >= self.meta.expected

    @property
    def active(self) -> ExecutionRecord | None:
        return next((r for r in self.records if r.role is ExecutionRole.ACTIVE), None)

    @property
    def shadows(self) -> tuple[ExecutionRecord, ...]:
        return tuple(r for r in self.records if r.role is ExecutionRole.SHADOW)


@runtime_checkable
class ShadowObserver(Protocol):
    """Receives comparison lifecycle events; the hook for telemetry and persistence.

    Both methods are called synchronously on the event loop, so they must be
    fast and must not block (queue the work or update in-memory counters). An
    exception from an observer is logged by type and dropped; it never affects
    the active response, other observers or other executions.
    """

    def comparison_started(self, comparison: ComparisonMeta) -> None: ...

    def execution_finished(self, record: ExecutionRecord) -> None: ...


class ShadowStore:
    """Bounded in-memory :class:`ShadowObserver`: the newest comparisons win.

    At most ``max_comparisons`` comparisons are kept (oldest evicted first) and
    each holds at most ``expected`` records, so memory is bounded by
    ``max_comparisons * (1 + MAX_SHADOW_TARGETS)`` canonical responses, each of
    which is itself size-bounded by the contract. A record that arrives for an
    already evicted comparison is dropped and counted, never resurrected.
    Thread-safe, so a test or a future admin surface can read from another
    thread than the event loop.
    """

    def __init__(self, max_comparisons: int) -> None:
        if max_comparisons < 1:
            raise ValueError("max_comparisons must be at least 1")
        self._max = max_comparisons
        self._lock = threading.Lock()
        self._metas: OrderedDict[str, ComparisonMeta] = OrderedDict()
        self._records: dict[str, list[ExecutionRecord]] = {}
        self._evicted = 0
        self._dropped_records = 0

    def comparison_started(self, comparison: ComparisonMeta) -> None:
        with self._lock:
            self._metas[comparison.comparison_id] = comparison
            self._records[comparison.comparison_id] = []
            while len(self._metas) > self._max:
                oldest, _ = self._metas.popitem(last=False)
                del self._records[oldest]
                self._evicted += 1

    def execution_finished(self, record: ExecutionRecord) -> None:
        with self._lock:
            meta = self._metas.get(record.comparison_id)
            records = self._records.get(record.comparison_id)
            if meta is None or records is None or len(records) >= meta.expected:
                self._dropped_records += 1
                return
            records.append(record)

    def get(self, comparison_id: str) -> ComparisonSnapshot | None:
        with self._lock:
            meta = self._metas.get(comparison_id)
            if meta is None:
                return None
            return ComparisonSnapshot(meta, tuple(self._records[comparison_id]))

    def recent(self, limit: int = 20) -> list[ComparisonSnapshot]:
        """The newest comparisons first, at most ``limit`` of them."""
        if limit <= 0:
            return []
        with self._lock:
            ids = list(self._metas)[-limit:][::-1]
            return [ComparisonSnapshot(self._metas[i], tuple(self._records[i])) for i in ids]

    def __len__(self) -> int:
        with self._lock:
            return len(self._metas)

    @property
    def evicted(self) -> int:
        """Comparisons pushed out by newer ones since the store was created."""
        return self._evicted

    @property
    def dropped_records(self) -> int:
        """Records that arrived for an evicted (or already full) comparison."""
        return self._dropped_records


class _Run:
    """Bookkeeping for one shadow execution; ``emitted`` guards the one-record rule."""

    __slots__ = ("comparison", "emitted", "model", "provider", "registered", "started", "t0")

    def __init__(self, comparison: ShadowComparison, target: ShadowTarget, registered: bool):
        self.comparison = comparison
        self.provider = target.provider
        self.model = target.model
        self.registered = registered
        self.started = datetime.now(UTC)
        self.t0 = time.perf_counter()
        self.emitted = False


class ShadowComparison:
    """Handle for one generate call: records the active outcome, owns its shadow tasks."""

    def __init__(
        self,
        evaluator: ShadowEvaluator,
        meta: ComparisonMeta,
        *,
        provider: str,
        model: str,
    ) -> None:
        self._evaluator = evaluator
        self.meta = meta
        self._provider = provider
        self._model = model
        self._started = datetime.now(UTC)
        self._t0 = time.perf_counter()
        self._active_emitted = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def comparison_id(self) -> str:
        return self.meta.comparison_id

    def finish_active(self, outcome: GenerationOutcome) -> None:
        """Record the canonical active outcome (a private copy: callers may mutate theirs)."""
        stored = replace(
            outcome,
            response=outcome.response.model_copy(deep=True),
            comparison_id=self.comparison_id,
        )
        status = ExecutionStatus.SUCCESS if outcome.response.success else ExecutionStatus.FAILURE
        self._emit_active(status, stored)

    def abort(self, *, cancelled: bool) -> None:
        """The active call ended without an outcome: record it and stop the shadows.

        Purely synchronous, so it can run inside a ``CancelledError`` handler
        without delaying the cancellation.
        """
        self._emit_active(ExecutionStatus.CANCELLED if cancelled else ExecutionStatus.ERROR, None)
        for task in self._tasks:
            task.cancel()

    def _emit_active(self, status: ExecutionStatus, outcome: GenerationOutcome | None) -> None:
        if self._active_emitted:
            return
        self._active_emitted = True
        self._evaluator._emit(
            ExecutionRecord(
                comparison_id=self.comparison_id,
                role=ExecutionRole.ACTIVE,
                provider=self._provider,
                model=self._model,
                registered=True,  # comparisons only start after a successful selection
                status=status,
                request_id=self.meta.request_id,
                run_id=self.meta.run_id,
                started_at=self._started,
                completed_at=datetime.now(UTC),
                duration_ms=_elapsed_ms(self._t0),
                outcome=outcome,
            )
        )


class ShadowEvaluator:
    """Fans one request out to the configured shadow targets and records everything."""

    def __init__(
        self,
        settings: ShadowSettings,
        registry: ProviderRegistry,
        runner: ShadowRunner,
        *,
        default_timeout: float,
        observers: Sequence[ShadowObserver] = (),
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._runner = runner
        self._timeout = settings.timeout_seconds or default_timeout
        self.store = ShadowStore(settings.store_size)
        self._observers: tuple[ShadowObserver, ...] = (self.store, *observers)
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._warned: set[tuple[str, str | None, SkipReason]] = set()

    @property
    def enabled(self) -> bool:
        return self._settings.enabled and not self._closed

    @property
    def targets(self) -> tuple[ShadowTarget, ...]:
        """Every configured target, registered or not (internal use)."""
        return self._settings.targets

    @property
    def registered_targets(self) -> tuple[ShadowTarget, ...]:
        """Configured targets whose provider (and model) the registry knows.

        The only targets safe to show to API clients: an unregistered value is
        arbitrary operator input and could be a misplaced credential.
        """
        return tuple(
            t for t in self._settings.targets if self._registry.is_registered(t.provider, t.model)
        )

    @property
    def rejected_config_entries(self) -> int:
        return self._settings.rejected

    @property
    def pending(self) -> int:
        """Shadow calls currently running."""
        return len(self._tasks)

    def check_targets(self) -> None:
        """Warn (once per target) about configured targets that cannot run right now."""
        for target in self._settings.targets:
            reason = self._selection_problem(target)
            if reason is not None:
                self._warn_skip(target, reason)

    def begin(
        self, request: GenerationRequest, *, provider: str, model: str, parent_context: Any = None
    ) -> ShadowComparison:
        """Start a comparison and launch every shadow target. Synchronous and non-blocking.

        Call from the active path right after the active provider was selected
        and before it is awaited, so shadows overlap the active call.
        """
        meta = ComparisonMeta(
            comparison_id=f"cmp-{uuid.uuid4().hex}",
            request_id=request.request_id,
            run_id=request.run_id,
            created_at=datetime.now(UTC),
            expected=1 + len(self._settings.targets),
            parent_context=parent_context,
        )
        comparison = ShadowComparison(self, meta, provider=provider, model=model)
        self._notify(lambda o: o.comparison_started(meta), "comparison_started")
        for target in self._settings.targets:
            try:
                self._launch(comparison, request, target)
            except Exception as exc:  # one broken target must not affect the others
                logger.error("shadow launch failed: %s", type(exc).__name__)
                self._record_skip(comparison, target, SkipReason.LAUNCH_FAILED, registered=False)
        return comparison

    async def aclose(self) -> None:
        """Stop accepting shadow work and end every running shadow call, deterministically.

        Waits up to ``drain_seconds`` for running calls to finish on their own,
        then cancels the rest and waits a short bounded grace for the
        cancellations to land. A call that still refuses to die (an adapter that
        swallows cancellation) is logged and abandoned rather than hanging
        shutdown. If ``aclose`` itself is cancelled, every shadow call is
        cancelled before the cancellation propagates.
        """
        self._closed = True
        running = set(self._tasks)
        if not running:
            return
        try:
            _, unfinished = await asyncio.wait(running, timeout=self._settings.drain_seconds)
            if unfinished:
                for task in unfinished:
                    task.cancel()
                _, stuck = await asyncio.wait(unfinished, timeout=_CANCEL_GRACE_SECONDS)
                if stuck:
                    logger.error("%d shadow call(s) ignored cancellation at shutdown", len(stuck))
        except asyncio.CancelledError:
            for task in running:
                task.cancel()
            raise

    # -- internals ---------------------------------------------------------

    def _selection_problem(self, target: ShadowTarget) -> SkipReason | None:
        try:
            self._registry.select(target.provider, target.model)
        except ProviderSelectionError as exc:
            return SkipReason(exc.reason.value)
        return None

    def _launch(
        self, comparison: ShadowComparison, request: GenerationRequest, target: ShadowTarget
    ) -> None:
        registered = self._registry.is_registered(target.provider, target.model)
        if self._closed:
            self._record_skip(comparison, target, SkipReason.SHUTTING_DOWN, registered)
            return
        if len(self._tasks) >= self._settings.max_in_flight:
            self._record_skip(comparison, target, SkipReason.OVERLOADED, registered)
            return
        problem = self._selection_problem(target)
        if problem is not None:
            self._warn_skip(target, problem)
            self._record_skip(comparison, target, problem, registered)
            return
        # Deep copy now, synchronously: whatever a shadow does to its request
        # later, the active provider and the other shadows never see it.
        copied = request.model_copy(deep=True)
        run = _Run(comparison, target, registered)
        task = asyncio.create_task(
            self._run(run, copied),
            name=f"shadow:{comparison.comparison_id}:{target.provider}",
        )
        self._tasks.add(task)
        comparison._tasks.append(task)
        task.add_done_callback(lambda done, run=run: self._on_done(done, run))

    async def _run(self, run: _Run, request: GenerationRequest) -> None:
        # Local import avoids a cycle: the observer consumes these record types.
        from dungeon_director.comparison_telemetry import telemetry_context

        with telemetry_context(
            shadow_comparison_id=run.comparison.comparison_id,
            execution_mode="shadow",
            parent_context=run.comparison.meta.parent_context,
        ):
            outcome = await self._runner(request, run.provider, run.model, self._timeout)
        status = ExecutionStatus.SUCCESS if outcome.response.success else ExecutionStatus.FAILURE
        self._emit_shadow(run, status, replace(outcome, comparison_id=run.comparison.comparison_id))

    def _on_done(self, task: asyncio.Task[None], run: _Run) -> None:
        """Runs for every shadow task, however it ended: consume the exception, close the books."""
        self._tasks.discard(task)
        cancelled = task.cancelled()
        error = None if cancelled else task.exception()
        if error is not None:
            # Type only: the message may carry adapter or credential text.
            logger.error(
                "shadow run %s/%s raised %s", run.provider, run.model, type(error).__name__
            )
        if not run.emitted:  # cancelled (even before its first step) or crashed
            self._emit_shadow(
                run, ExecutionStatus.CANCELLED if cancelled else ExecutionStatus.ERROR, None
            )

    def _emit_shadow(
        self, run: _Run, status: ExecutionStatus, outcome: GenerationOutcome | None
    ) -> None:
        if run.emitted:
            return
        run.emitted = True
        comparison = run.comparison
        model = outcome.response.metadata.model if outcome is not None else run.model
        self._emit(
            ExecutionRecord(
                comparison_id=comparison.comparison_id,
                role=ExecutionRole.SHADOW,
                provider=run.provider,
                model=model,
                registered=run.registered,
                status=status,
                request_id=comparison.meta.request_id,
                run_id=comparison.meta.run_id,
                started_at=run.started,
                completed_at=datetime.now(UTC),
                duration_ms=_elapsed_ms(run.t0),
                outcome=outcome,
            )
        )

    def _record_skip(
        self,
        comparison: ShadowComparison,
        target: ShadowTarget,
        reason: SkipReason,
        registered: bool,
    ) -> None:
        now = datetime.now(UTC)
        self._emit(
            ExecutionRecord(
                comparison_id=comparison.comparison_id,
                role=ExecutionRole.SHADOW,
                provider=target.provider,
                model=target.model,
                registered=registered,
                status=ExecutionStatus.SKIPPED,
                request_id=comparison.meta.request_id,
                run_id=comparison.meta.run_id,
                started_at=now,
                completed_at=now,
                duration_ms=0.0,
                skip_reason=reason,
            )
        )

    def _warn_skip(self, target: ShadowTarget, reason: SkipReason) -> None:
        key = (target.provider, target.model, reason)
        if key in self._warned:
            return
        self._warned.add(key)
        # A value that is not a registered provider or model may be anything an
        # operator pasted into the variable, credentials included, so it is
        # never printed: the warning says what kind of problem it is instead.
        if reason is SkipReason.UNKNOWN_PROVIDER:
            what = "a target naming an unregistered provider"
        elif reason is SkipReason.UNKNOWN_MODEL:
            what = f"a {target.provider} target naming an unregistered model"
        else:
            what = f"shadow target {target.provider}/{target.model or 'default'}"
        logger.warning("shadow: %s cannot run: %s", what, reason.value)

    def _emit(self, record: ExecutionRecord) -> None:
        self._notify(lambda o: o.execution_finished(record), "execution_finished")

    def _notify(self, call: Callable[[ShadowObserver], None], what: str) -> None:
        for observer in self._observers:
            try:
                call(observer)
            except Exception as exc:  # observers are untrusted: never let one break a request
                logger.error(
                    "shadow observer %s.%s failed: %s",
                    type(observer).__name__,
                    what,
                    type(exc).__name__,
                )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
