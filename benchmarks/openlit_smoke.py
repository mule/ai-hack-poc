"""Emit real director telemetry and verify fresh ClickHouse ingestion (issue #28)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

SIGNALS = ("traces", "logs", "counter", "histogram")
LIVE_PROVIDERS = ("typesafe-jev", "cloudflare-jev", "groq", "cerebras")


class SmokeError(Exception):
    """A fixed safe error code, never an underlying exception/response body."""


@dataclass(frozen=True)
class Sample:
    request_id: str
    provider: str


class ClickHouseVerifier:
    """SELECT-only verifier. Authentication comes only from explicit environment config."""

    def __init__(
        self, env: dict[str, str], *, transport: httpx.BaseTransport | None = None
    ):
        url = env.get("OPENLIT_SMOKE_CLICKHOUSE_URL", "")
        database = env.get("OPENLIT_SMOKE_CLICKHOUSE_DATABASE", "")
        user = env.get("OPENLIT_SMOKE_CLICKHOUSE_USER", "")
        password = env.get("OPENLIT_SMOKE_CLICKHOUSE_PASSWORD")
        try:
            parts = urlsplit(url)
            valid_url = parts.scheme in {"http", "https"} and bool(parts.hostname)
            valid_url = valid_url and not parts.username and not parts.password
            valid_url = valid_url and not parts.query and not parts.fragment
        except ValueError:
            valid_url = False
        if not valid_url or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
            raise SmokeError("verification_config_missing_or_invalid")
        if not user or password is None:
            raise SmokeError("verification_credentials_missing")
        self.url, self.database = url, database
        self.client = httpx.Client(
            auth=(user, password),
            timeout=5,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def counts(self, instance: str, started_ms: int, sample: Sample) -> dict[str, int]:
        # Identifiers and table names are fixed. All invocation data use typed parameters.
        trace_where = """ResourceAttributes['service.instance.id'] = {instance:String}
            AND SpanAttributes['director.request_id'] = {request:String}
            AND SpanName = 'director.generate'
            AND Timestamp >= fromUnixTimestamp64Milli({started:Int64})"""
        sql = f"""SELECT 'traces' AS signal, count() AS matched FROM otel_traces
            WHERE {trace_where}
            UNION ALL SELECT 'logs', count() FROM otel_logs
            WHERE ResourceAttributes['service.instance.id'] = {{instance:String}}
              AND LogAttributes['director.request_id'] = {{request:String}}
              AND Body = 'director.smoke.generation'
              AND TraceId != ''
              AND TraceId IN (SELECT TraceId FROM otel_traces WHERE {trace_where})
              AND Timestamp >= fromUnixTimestamp64Milli({{started:Int64}})
            UNION ALL SELECT 'counter', count() FROM otel_metrics_sum
            WHERE ResourceAttributes['service.instance.id'] = {{instance:String}}
              AND MetricName = 'director.generation.requests'
              AND Attributes['provider'] = {{provider:String}} AND Value >= 1
              AND Attributes['status'] = 'success'
              AND TimeUnix >= fromUnixTimestamp64Milli({{started:Int64}})
            UNION ALL SELECT 'histogram', count() FROM otel_metrics_histogram
            WHERE ResourceAttributes['service.instance.id'] = {{instance:String}}
              AND MetricName = 'director.generation.duration' AND Count >= 1
              AND Attributes['provider'] = {{provider:String}}
              AND Attributes['status'] = 'success'
              AND TimeUnix >= fromUnixTimestamp64Milli({{started:Int64}})
            FORMAT JSON"""
        try:
            response = self.client.post(
                self.url,
                params={
                    "database": self.database,
                    "readonly": "1",
                    "max_execution_time": "3",
                    "param_instance": instance,
                    "param_started": str(started_ms),
                    "param_request": sample.request_id,
                    "param_provider": sample.provider,
                },
                content=sql.encode(),
            )
            if response.status_code in (401, 403):
                raise SmokeError("verification_auth_failed")
            response.raise_for_status()
            rows = response.json()["data"]
            result = {row["signal"]: int(row["matched"]) for row in rows}
            if set(result) != set(SIGNALS) or any(n < 0 for n in result.values()):
                raise ValueError
            return result
        except SmokeError:
            raise
        except Exception:  # noqa: BLE001 - external errors must not disclose credentials
            raise SmokeError("verification_query_failed") from None


def verify(
    verifier: ClickHouseVerifier,
    instance: str,
    started_ms: int,
    samples: list[Sample],
    deadline_seconds: float,
) -> dict[str, dict[str, int]]:
    deadline = time.monotonic() + deadline_seconds
    counts: dict[str, dict[str, int]] = {}
    while True:
        for sample in samples:
            counts[sample.request_id] = verifier.counts(instance, started_ms, sample)
        if all(all(row[name] > 0 for name in SIGNALS) for row in counts.values()):
            return counts
        if time.monotonic() >= deadline:
            return counts
        time.sleep(min(1, max(0, deadline - time.monotonic())))


async def emit(telemetry: Any, providers: list[str], instance: str) -> list[Sample]:
    from dungeon_director.contracts import GenerationRequest
    from dungeon_director.registry import default_registry
    from dungeon_director.service import DirectorService
    from dungeon_director.settings import DirectorSettings

    registry = default_registry()
    # Explicit settings prevent ambient shadow config from making extra billable calls.
    service = DirectorService(
        registry, DirectorSettings(timeout_seconds=30), telemetry=telemetry
    )
    tracer = telemetry.tracer_provider.get_tracer("dungeon-director-smoke")
    fixture = (
        Path(__file__).resolve().parents[1]
        / "contracts/fixtures/generation_request.json"
    )
    payload = json.loads(fixture.read_text())
    samples = []
    try:
        for index, provider in enumerate(providers):
            request_id = f"smoke-{instance}-{index}"
            request = GenerationRequest.model_validate(
                {
                    **payload,
                    "request_id": request_id,
                    "run_id": f"smoke-{instance}",
                }
            )
            with tracer.start_as_current_span("director.smoke"):
                outcome = await service.generate(request, provider=provider)
                telemetry.emit_log(
                    "director.smoke.generation",
                    {
                        "director.request_id": request_id,
                        "director.run_id": request.run_id,
                        "director.provider": provider,
                        "director.status": "success"
                        if outcome.response.success
                        else "failed",
                    },
                )
            if not outcome.response.success:
                raise SmokeError("generation_failed")
            samples.append(Sample(request_id, provider))
    finally:
        await registry.aclose()
    return samples


def run(args: argparse.Namespace, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    from dungeon_director.telemetry import TelemetrySettings, setup_telemetry

    evidence: dict[str, Any] = {"status": "failed", "ingestion_verified": False}
    verifier = None
    telemetry = None
    try:
        if args.live_provider and not args.live:
            raise SmokeError("live_provider_requires_live_opt_in")
        if not args.emit_only:
            verifier = ClickHouseVerifier(env)
        settings = TelemetrySettings.from_env(env)
        if not settings.enabled or not all(
            settings.signal_enabled(s) for s in ("traces", "metrics", "logs")
        ):
            raise SmokeError("all_three_export_signals_required")
        instance = uuid.uuid4().hex
        resource = dict(settings.resource_attributes)
        resource["service.instance.id"] = instance
        settings = replace(settings, resource_attributes=tuple(resource.items()))
        telemetry = setup_telemetry(settings)
        if not telemetry.enabled:
            raise SmokeError("export_setup_failed")
        started_ms = time.time_ns() // 1_000_000
        samples = asyncio.run(
            emit(
                telemetry,
                list(dict.fromkeys(["rules-baseline", *args.live_provider])),
                instance,
            )
        )
        telemetry.flush(5000)
        evidence.update(
            {
                "instance_id": instance,
                "started_ms": started_ms,
                "samples": [
                    {"request_id": s.request_id, "provider": s.provider}
                    for s in samples
                ],
            }
        )
        if args.emit_only:
            evidence["status"] = "emitted_unverified"
            # Deliberately nonzero: emission is not end-to-end acceptance.
            return 2, evidence
        counts = verify(verifier, instance, started_ms, samples, args.deadline)
        missing = {
            request: [name for name in SIGNALS if row[name] <= 0]
            for request, row in counts.items()
        }
        evidence["counts"] = counts
        evidence["missing"] = {
            request: names for request, names in missing.items() if names
        }
        if evidence["missing"]:
            evidence["error"] = "ingestion_missing_signals"
            return 1, evidence
        evidence.update(status="passed", ingestion_verified=True)
        return 0, evidence
    except SmokeError as exc:
        evidence["error"] = str(exc)
        return 1, evidence
    except Exception:  # noqa: BLE001 - external errors must not disclose credentials
        evidence["error"] = "smoke_failed"
        return 1, evidence
    finally:
        if verifier is not None:
            verifier.close()
        if telemetry is not None:
            worker = threading.Thread(target=telemetry.shutdown, daemon=True)
            worker.start()
            worker.join(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emit-only", action="store_true", help="Export evidence; exits 2, NOT a pass"
    )
    parser.add_argument(
        "--live", action="store_true", help="Opt in to billable provider calls"
    )
    parser.add_argument(
        "--live-provider", action="append", choices=LIVE_PROVIDERS, default=[]
    )
    parser.add_argument(
        "--deadline", type=float, default=60, help="Ingestion polling seconds (1-300)"
    )
    args = parser.parse_args(argv)
    if not 1 <= args.deadline <= 300:
        parser.error("deadline must be between 1 and 300 seconds")
    # CLI evidence is the sole diagnostic channel. SDK/provider messages may
    # contain collector response text; never forward it into smoke artifacts.
    logging.disable(logging.CRITICAL)
    code, evidence = run(args, dict(os.environ))
    print(json.dumps(evidence, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
