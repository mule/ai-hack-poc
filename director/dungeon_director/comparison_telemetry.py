"""Bounded metadata-only comparisons; canonical outcomes remain in ShadowStore."""

from __future__ import annotations

import math
import re
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

from dungeon_director.shadow import ComparisonMeta, ExecutionRecord, ExecutionRole

if TYPE_CHECKING:
    from dungeon_director.telemetry import DirectorTelemetry

_CORRELATION: ContextVar[dict[str, str] | None] = ContextVar("comparison_correlation", default=None)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_KEYS = {
    "shadow_comparison_id",
    "replay_id",
    "evaluation_id",
    "dataset_id",
    "case_id",
    "dataset_version",
}


def current_correlation() -> dict[str, str]:
    return dict(_CORRELATION.get() or {})


@contextmanager
def telemetry_context(**attributes: str):
    """Task-local correlation inherited by shadow tasks, never metric labels."""
    values = current_correlation()
    for key, value in attributes.items():
        if key == "execution_mode" and value in {"active", "shadow", "replay"}:
            values[key] = value
        elif key in _KEYS and isinstance(value, str) and _ID.fullmatch(value):
            values[key] = value
    token = _CORRELATION.set(values)
    try:
        yield values
    finally:
        _CORRELATION.reset(token)


def _winner(active: float | None, shadow: float | None) -> str:
    if active is None or shadow is None or not math.isfinite(active) or not math.isfinite(shadow):
        return "unknown"
    return "tie" if active == shadow else "active" if active < shadow else "shadow"


def _delta(active: float, shadow: float) -> str:
    difference = shadow - active
    return "equal" if abs(difference) < 1e-9 else "higher" if difference > 0 else "lower"


def comparison_summary(active: ExecutionRecord, shadow: ExecutionRecord) -> dict[str, str]:
    """Closed-value summaries; absence never masquerades as a zero measurement."""
    from dungeon_director.telemetry import _SCHEMA_CODES

    a = active.outcome.response if active.outcome else None
    b = shadow.outcome.response if shadow.outcome else None
    result = {"success_mismatch": "unknown", "schema_mismatch": "unknown"}
    if a is not None and b is not None:
        result["success_mismatch"] = str(a.success != b.success).lower()
        ae = a.metadata.error.code.value if a.metadata.error else None
        be = b.metadata.error.code.value if b.metadata.error else None
        result["schema_mismatch"] = str((ae in _SCHEMA_CODES) != (be in _SCHEMA_CODES)).lower()
    result["latency_winner"] = _winner(
        active.duration_ms if a is not None else None,
        shadow.duration_ms if b is not None else None,
    )
    result["cost_winner"] = _winner(
        a.metadata.usage.estimated_cost_usd if a and a.metadata.usage else None,
        b.metadata.usage.estimated_cost_usd if b and b.metadata.usage else None,
    )
    ar, br = a.room if a else None, b.room if b else None
    for name in ("room_type", "size", "has_secret"):
        av, bv = getattr(ar, name, None), getattr(br, name, None)
        result[f"{name}_mismatch"] = (
            "unknown" if av is None or bv is None else str(av != bv).lower()
        )
    for name in ("danger", "enemy_density", "loot_density", "secret_probability"):
        result[f"{name}_delta"] = (
            _delta(getattr(ar, name), getattr(br, name)) if ar and br else "unknown"
        )
    result["exit_count_delta"] = _delta(len(ar.exits), len(br.exits)) if ar and br else "unknown"
    return result


@dataclass
class _Pending:
    expected: int
    context: Any
    correlation: dict[str, str]
    records: list[ExecutionRecord] = field(default_factory=list)
    paired: set[int] = field(default_factory=set)


class ComparisonTelemetry:
    """ShadowObserver using bounded state and the application's batch exporters.

    Observer exceptions are isolated by ShadowEvaluator. No remote I/O or waits
    occur here: production SDK processors queue spans/logs for background export.
    """

    def __init__(self, telemetry: DirectorTelemetry, max_comparisons: int = 256):
        if not 1 <= max_comparisons <= 1024:
            raise ValueError("max_comparisons must be between 1 and 1024")
        self._telemetry = telemetry
        self._max = max_comparisons
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._tracer = telemetry.tracer_provider.get_tracer(__name__)
        meter = telemetry.meter_provider.get_meter(__name__)
        self._executions = meter.create_counter("director.shadow.executions")
        self._pairs = meter.create_counter("director.shadow.comparisons")
        self._evicted = meter.create_counter("director.shadow.comparisons.evicted")

    def comparison_started(self, comparison: ComparisonMeta) -> None:
        if not self._telemetry.enabled:
            return
        self._pending[comparison.comparison_id] = _Pending(
            min(comparison.expected, 5),
            trace.set_span_in_context(trace.get_current_span()),
            current_correlation(),
        )
        while len(self._pending) > self._max:
            self._pending.popitem(last=False)
            self._evicted.add(1)

    def execution_finished(self, record: ExecutionRecord) -> None:
        if not self._telemetry.enabled:
            return
        labels = record.metric_labels()
        self._executions.add(1, labels)
        pending = self._pending.get(record.comparison_id)
        attrs = {
            **(pending.correlation if pending else {}),
            **labels,
            "shadow_comparison_id": record.comparison_id,
            "request_id": record.request_id,
            "run_id": record.run_id,
            "director.request_id": record.request_id,
            "director.run_id": record.run_id,
        }
        with self._tracer.start_as_current_span(
            "director.shadow.execution",
            context=pending.context if pending else None,
            attributes=attrs,
        ):
            self._telemetry.emit_log("shadow execution completed", attrs)
        if pending is None or len(pending.records) >= pending.expected:
            return
        pending.records.append(record)
        active = next((r for r in pending.records if r.role is ExecutionRole.ACTIVE), None)
        if active is not None:
            for index, shadow in enumerate(pending.records):
                if shadow.role is not ExecutionRole.SHADOW or index in pending.paired:
                    continue
                pending.paired.add(index)
                active_labels, shadow_labels = active.metric_labels(), shadow.metric_labels()
                pair = {
                    "active_provider": active_labels["provider"],
                    "active_model": active_labels["model"],
                    "shadow_provider": shadow_labels["provider"],
                    "shadow_model": shadow_labels["model"],
                    **comparison_summary(active, shadow),
                }
                # One closed category/result per observation avoids the enormous
                # Cartesian product of all room differences as metric labels.
                dimensions = {
                    key: pair[key]
                    for key in (
                        "active_provider",
                        "active_model",
                        "shadow_provider",
                        "shadow_model",
                    )
                }
                for category, result in comparison_summary(active, shadow).items():
                    self._pairs.add(1, {**dimensions, "category": category, "result": result})
                with self._tracer.start_as_current_span(
                    "director.shadow.comparison",
                    context=pending.context,
                    attributes={**attrs, **pair},
                ):
                    self._telemetry.emit_log("shadow comparison completed", {**attrs, **pair})
        if len(pending.records) >= pending.expected:
            del self._pending[record.comparison_id]
