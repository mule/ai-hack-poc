"""Bounded, observation-only ingestion of Godot lifecycle events.

The game sends small batches independently of generation. This bridge uses the
director's exporter providers, so the client needs neither OTLP nor credentials.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Link
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from dungeon_director.telemetry import DirectorTelemetry

logger = logging.getLogger(__name__)
MAX_GAME_BODY_BYTES = 64 * 1024


def game_telemetry_router(
    telemetry: DirectorTelemetry, registered: Mapping[str, set[str]]
) -> APIRouter:
    """Build an isolated bridge with bounded ingestion and safe error responses."""
    from dungeon_director.telemetry_schema import GameEventBatch

    router = APIRouter()
    budget = EventBudget()
    try:
        recorder = GameTelemetryRecorder(telemetry, registered)
    except Exception as exc:
        logger.warning("game telemetry setup failed (%s)", type(exc).__name__)
        recorder = None

    @router.post("/v1/telemetry/game")
    async def ingest(request: Request) -> JSONResponse:
        # Validate the actual streamed length, not the untrusted Content-Length.
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_GAME_BODY_BYTES:
                return JSONResponse({"error": "batch_too_large"}, status_code=413)
            body.extend(chunk)
        try:
            batch = GameEventBatch.model_validate_json(bytes(body))
        except (ValueError, TypeError):
            # Pydantic error representations contain submitted values. Never
            # return/log them for this telemetry boundary.
            return JSONResponse({"error": "invalid_game_telemetry"}, status_code=422)
        if not budget.accept(len(batch.events)):
            return JSONResponse({"error": "telemetry_rate_limited"}, status_code=429)
        if recorder is not None:
            for event in batch.events:
                recorder.record(event.model_dump(mode="json", exclude_none=True))
        return JSONResponse({"accepted": len(batch.events)}, status_code=202)

    return router


class EventBudget:
    """One fixed-memory process-wide token bucket, independent of client IDs."""

    def __init__(self, capacity: int = 512, rate: float = 128.0) -> None:
        self.capacity = capacity
        self.rate = rate
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def accept(self, count: int) -> bool:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if count > self._tokens:
                return False
            self._tokens -= count
            return True


class GameTelemetryRecorder:
    """Export prevalidated events without blocking on collector delivery.

    IDs remain on spans/logs. Only a fixed event enum and registry-resolved
    provider/model identities may create metric series.
    """

    def __init__(self, telemetry: DirectorTelemetry, registered: Mapping[str, set[str]]) -> None:
        self.telemetry = telemetry
        self.registered = registered
        self._warned = False
        self._tracer = telemetry.tracer_provider.get_tracer("dungeon-game-bridge")
        meter = telemetry.meter_provider.get_meter("dungeon-game-bridge")
        self._events = meter.create_counter("game.lifecycle.events", unit="1")
        self._duration = meter.create_histogram("game.lifecycle.duration", unit="s")
        self._rooms = meter.create_counter("game.rooms.committed", unit="1")
        self._density = meter.create_histogram("game.room.density", unit="1")

    def record(self, event: Mapping[str, Any]) -> None:
        """Record a validated event. Exporter failures never escape this boundary."""
        try:
            self._record(event)
        except Exception as exc:
            # No exception text: exporter errors can include credentials/content.
            if not self._warned:
                logger.warning("game telemetry recording failed (%s)", type(exc).__name__)
                self._warned = True

    def _record(self, event: Mapping[str, Any]) -> None:
        name = event["event_name"]
        attributes = dict(event.get("attributes", {}))
        attributes.update(
            {
                "game.event.name": name,
                "director.run_id": event["run_id"],
                "telemetry.schema.version": "1",
            }
        )
        if event.get("request_id"):
            attributes["director.request_id"] = event["request_id"]
        dimensions = {"event_name": name}
        provider = attributes.get("provider")
        model = attributes.get("model")
        if provider in self.registered and model in self.registered[provider]:
            dimensions.update(provider=provider, model=model)
        else:
            dimensions.update(provider="unknown", model="unknown")

        links = []
        traceparent = event.get("traceparent")
        if traceparent:
            context = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
            span_context = trace.get_current_span(context).get_span_context()
            if span_context.is_valid:
                links.append(Link(span_context))
        # These are point-in-time events. A short linked span makes the event
        # searchable even in OpenLIT installations whose log explorer is disabled.
        now = time.time_ns()
        # Preserve producer wall time for offline/batched events; duration fields
        # are measured with the game's monotonic clock, not cross-host subtraction.
        if event.get("timestamp"):
            observed = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            attributes["game.event.timestamp"] = observed.isoformat()
        span = self._tracer.start_span(
            "game.lifecycle", attributes=attributes, links=links, start_time=now
        )
        try:
            span.add_event(name, attributes=attributes, timestamp=now)
            emit = getattr(self.telemetry, "emit_log", None)
            if emit is not None:
                emit(name, attributes, context=trace.set_span_in_context(span))
        finally:
            span.end(end_time=now)
        self._events.add(1, dimensions)
        if name == "room.committed":
            room_dimensions = dict(dimensions)
            for key in ("room_type", "room_size", "danger", "exit_count", "has_secret"):
                if key in attributes:
                    room_dimensions[key] = attributes[key]
            self._rooms.add(1, room_dimensions)
            for key in ("enemy_density", "loot_density"):
                if key in attributes:
                    self._density.record(attributes[key], {**dimensions, "density_type": key})
        for key in (
            "duration_ms",
            "queue_ms",
            "network_ms",
            "materialization_ms",
            "time_to_visible_ms",
            "time_to_entry_ms",
        ):
            value = attributes.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._duration.record(value / 1000.0, {**dimensions, "stage": key})
