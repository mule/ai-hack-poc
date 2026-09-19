# Observability

Status: **placeholder.** No configuration exists yet.

Purpose (epic #1): make every generation decision measurable. The director is
intended to record, per decision: latency, provider/model, success or failure,
schema validity, token usage where available, and estimated cost where
available.

Planned scope, delivered by the telemetry issue:

- OpenTelemetry instrumentation in the director, exported over OTLP
  (`OTEL_EXPORTER_OTLP_ENDPOINT` in the root `.env.example`).
- OpenLIT-oriented local configuration for viewing LLM traces and metrics.
- A Docker Compose file for the local collector/dashboards, added here only if
  it helps local startup. The Godot client is never containerized.
