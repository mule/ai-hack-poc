"""Replay benchmark CLI and execution engine for dungeon generation (issue #14).

Replays recorded canonical generation events against selected director providers
(rules-baseline, typesafe-jev, cloudflare-jev, groq, cerebras) via DirectorService.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import sys
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dungeon_director.cerebras import (
    CEREBRAS_PROVIDER_ID,
    DEFAULT_CEREBRAS_MODEL,
    CerebrasConfig,
    CerebrasProvider,
    CerebrasTransport,
)
from dungeon_director.cloudflare_jev import (
    CLOUDFLARE_JEV_PROVIDER_ID,
    DEFAULT_JEV_MODEL,
    CloudflareJevProvider,
    JevConfig,
    JevTransport,
)
from dungeon_director.comparison_telemetry import current_correlation, telemetry_context
from dungeon_director.contracts import (
    CONTRACT_VERSION,
    ErrorKind,
    GenerationRequest,
    GenerationResponse,
)
from dungeon_director.groq import (
    DEFAULT_GROQ_MODEL,
    GROQ_PROVIDER_ID,
    GroqConfig,
    GroqProvider,
    GroqTransport,
)
from dungeon_director.registry import ProviderRegistry
from dungeon_director.rules import RULES_MODEL, RULES_PROVIDER_ID, RulesProvider
from dungeon_director.service import DirectorService, GenerationOutcome
from dungeon_director.settings import DirectorSettings
from dungeon_director.telemetry import (
    DirectorTelemetry,
    TelemetrySettings,
    setup_telemetry,
)
from opentelemetry import trace

SUPPORTED_PROVIDERS = (
    RULES_PROVIDER_ID,
    CLOUDFLARE_JEV_PROVIDER_ID,
    GROQ_PROVIDER_ID,
    CEREBRAS_PROVIDER_ID,
)

DEFAULT_MODELS: dict[str, str] = {
    RULES_PROVIDER_ID: RULES_MODEL,
    CLOUDFLARE_JEV_PROVIDER_ID: DEFAULT_JEV_MODEL,
    GROQ_PROVIDER_ID: DEFAULT_GROQ_MODEL,
    CEREBRAS_PROVIDER_ID: DEFAULT_CEREBRAS_MODEL,
}


def _resolve_benchmark_selections(
    providers: Sequence[str],
    models: dict[str, str | Sequence[str]] | None,
    *,
    iterations: int,
    concurrency: int,
) -> dict[str, list[str]]:
    """Validate one replay definition before constructing services or making calls."""
    for name, value in (("iterations", iterations), ("concurrency", concurrency)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")

    model_map: dict[str, list[str]] = {
        provider: [model] for provider, model in DEFAULT_MODELS.items()
    }
    for provider, selected in (models or {}).items():
        model_map[provider] = [selected] if isinstance(selected, str) else list(selected)

    seen: set[tuple[str, str]] = set()
    for provider in providers:
        for model in model_map.get(provider) or ["default"]:
            if (
                not isinstance(provider, str)
                or not provider
                or not isinstance(model, str)
                or not model
            ):
                raise ValueError("provider and model selections must be non-empty strings")
            selection = (provider, model)
            if selection in seen:
                raise ValueError(f"duplicate provider/model selection: {provider}/{model}")
            seen.add(selection)
    return model_map


@dataclass(frozen=True, slots=True)
class ReplayItemResult:
    """One evaluation of a generation request through the director service."""

    request_id: str
    run_id: str
    provider: str
    model: str
    iteration: int
    success: bool
    status_code: int
    latency_ms: float
    error_code: str | None = None
    error_message: str | None = None
    usage: dict[str, Any] | None = None
    room: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    # Retries the provider reported for this decision (None = not reported). The
    # director itself never retries, so this stays None unless a provider says so.
    retry_count: int | None = None
    telemetry_ids: dict[str, str] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "iteration": self.iteration,
            "success": self.success,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "usage": self.usage,
            "room": self.room,
            "provider_metadata": self.provider_metadata,
            "retry_count": self.retry_count,
            "telemetry_ids": self.telemetry_ids,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class ProviderSummary:
    """Aggregated metrics for one provider/model benchmark."""

    provider: str
    model: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    success_rate: float
    schema_failures: int
    timeouts: int
    rate_limits: int
    provider_errors: int
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_min_ms: float
    latency_max_ms: float
    latency_mean_ms: float
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_cost_usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Full benchmark report with machine-readable metrics and metadata."""

    generated_at: str
    contract_version: str
    input_file: str
    total_events_read: int
    unique_requests: int
    iterations: int
    concurrency: int
    providers: list[ProviderSummary]
    # Deprecated: providers replayed with several models are omitted; use `providers`.
    summary_by_provider: dict[str, dict[str, Any]]
    results: list[ReplayItemResult] = field(default_factory=list)

    def to_dict(self, *, include_results: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "generated_at": self.generated_at,
            "contract_version": self.contract_version,
            "input_file": self.input_file,
            "total_events_read": self.total_events_read,
            "unique_requests": self.unique_requests,
            "iterations": self.iterations,
            "concurrency": self.concurrency,
            "providers": [p.to_dict() for p in self.providers],
            "summary_by_provider": self.summary_by_provider,
        }
        if include_results:
            data["results"] = [r.to_dict() for r in self.results]
        return data


def percentile(values: Sequence[float], p: float) -> float:
    """Calculate percentile p (0.0 to 1.0) using linear interpolation."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return round(sorted_vals[0], 3)
    k = (n - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(sorted_vals[int(k)], 3)
    d0 = sorted_vals[int(f)] * (c - k)
    d1 = sorted_vals[int(c)] * (k - f)
    return round(d0 + d1, 3)


def load_dataset_requests(path: str | Path) -> list[GenerationRequest]:
    """Load canonical GenerationRequest objects from a JSON or JSONL file.

    Supports both raw GenerationRequest envelopes and recorded dataset events
    (which embed the request under the 'request' field).
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"dataset file not found: {file_path}")

    requests: list[GenerationRequest] = []
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return requests

    # If file begins with '[' or '{' and is single JSON object/array
    if text.startswith("["):
        raw_items = json.loads(text)
        if not isinstance(raw_items, list):
            raise ValueError(f"expected JSON list in {file_path}")
        for item in raw_items:
            req_dict = item.get("request", item) if isinstance(item, dict) else item
            requests.append(GenerationRequest.model_validate(req_dict))
        return requests

    # Line-delimited JSONL or single object
    for line_idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed JSON on line {line_idx} of {file_path}: {exc}") from exc

        if isinstance(parsed, dict):
            req_dict = parsed.get("request", parsed)
            requests.append(GenerationRequest.model_validate(req_dict))
        else:
            raise ValueError(f"expected JSON object on line {line_idx} of {file_path}")

    return requests


def build_default_service(
    *,
    jev_transport: JevTransport | None = None,
    groq_transport: GroqTransport | None = None,
    cerebras_transport: CerebrasTransport | None = None,
    timeout_seconds: float = 10.0,
    telemetry: DirectorTelemetry | None = None,
) -> tuple[DirectorService, ProviderRegistry]:
    """Construct a ProviderRegistry and DirectorService with all 4 providers.

    Injected transports enable 100% offline-safe replay and testing.
    """
    registry = ProviderRegistry()
    registry.register(RulesProvider())

    # Cloudflare Jev
    try:
        jev_provider = CloudflareJevProvider.from_env(transport=jev_transport)
        registry.register(jev_provider)
    except Exception:
        registry.register(CloudflareJevProvider(JevConfig(), transport=jev_transport))

    # Groq
    try:
        groq_provider = GroqProvider.from_env(transport=groq_transport)
        registry.register(groq_provider)
    except Exception:
        registry.register(GroqProvider(GroqConfig(), transport=groq_transport))

    # Cerebras
    try:
        cerebras_provider = CerebrasProvider.from_env(transport=cerebras_transport)
        registry.register(cerebras_provider)
    except Exception:
        registry.register(CerebrasProvider(CerebrasConfig(), transport=cerebras_transport))

    settings = DirectorSettings(
        default_provider=RULES_PROVIDER_ID,
        default_model=RULES_MODEL,
        timeout_seconds=timeout_seconds,
    )
    service = DirectorService(registry, settings, telemetry=telemetry)
    return service, registry


async def run_replay_item(
    service: DirectorService,
    request: GenerationRequest,
    provider: str,
    model: str,
    iteration: int,
) -> ReplayItemResult:
    """Execute one request against the director service."""
    # A case span is shared context for the service/provider spans; the artifact
    # also carries these IDs so operators can find this exact replay in OpenLIT.
    telemetry = service._telemetry
    correlation = {**current_correlation(), "execution_mode": "replay"}
    span_attributes = {
        **correlation,
        "director.request_id": request.request_id,
        "director.run_id": request.run_id,
    }
    span = None
    token = None
    try:
        span = telemetry.tracer_provider.get_tracer(__name__).start_span(
            "director.replay.case", attributes=span_attributes
        )
        from opentelemetry.context import attach

        token = attach(trace.set_span_in_context(span))
    except Exception:
        pass  # Telemetry failure must not change benchmark results.
    try:
        t0 = time.perf_counter()
        with telemetry_context(execution_mode="replay"):
            outcome: GenerationOutcome = await service.generate(
                request, provider=provider, model=model
            )
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 3)
        try:
            telemetry.emit_log("replay case completed", span_attributes)
        except Exception:
            pass
    finally:
        if token is not None:
            from opentelemetry.context import detach

            detach(token)
        if span is not None:
            try:
                span.end()
            except Exception:
                pass

    resp: GenerationResponse = outcome.response
    error_code = resp.metadata.error.code.value if resp.metadata.error else None
    error_msg = resp.metadata.error.message if resp.metadata.error else None

    usage_dict = (
        resp.metadata.usage.model_dump(mode="json", exclude_none=True)
        if resp.metadata.usage
        else None
    )
    room_dict = resp.room.model_dump(mode="json") if resp.room else None
    provider_metadata = dict(resp.metadata.provider_metadata or {})
    reported_retries = provider_metadata.get("retry_count")
    retry_count = (
        reported_retries
        if isinstance(reported_retries, int)
        and not isinstance(reported_retries, bool)
        and reported_retries >= 0
        else None
    )

    return ReplayItemResult(
        request_id=request.request_id,
        run_id=request.run_id,
        provider=provider,
        model=model,
        iteration=iteration,
        success=resp.success,
        status_code=outcome.status_code,
        latency_ms=elapsed_ms,
        error_code=error_code,
        error_message=error_msg,
        usage=usage_dict,
        room=room_dict,
        provider_metadata=provider_metadata,
        retry_count=retry_count,
        telemetry_ids={key: value for key, value in correlation.items() if key != "execution_mode"},
    )


def compute_provider_summary(
    provider: str, model: str, items: Sequence[ReplayItemResult]
) -> ProviderSummary:
    """Summarize latencies, error classifications, and tokens for a provider."""
    total = len(items)
    if total == 0:
        return ProviderSummary(
            provider=provider,
            model=model,
            total_requests=0,
            successful_requests=0,
            failed_requests=0,
            success_rate=0.0,
            schema_failures=0,
            timeouts=0,
            rate_limits=0,
            provider_errors=0,
            latency_p50_ms=0.0,
            latency_p90_ms=0.0,
            latency_p95_ms=0.0,
            latency_p99_ms=0.0,
            latency_min_ms=0.0,
            latency_max_ms=0.0,
            latency_mean_ms=0.0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_cost_usd=None,
        )

    successes = sum(1 for item in items if item.success)
    failures = total - successes
    success_rate = round(successes / total, 4)

    schema_failures = sum(
        1 for item in items if item.error_code == ErrorKind.SCHEMA_VIOLATION.value
    )
    timeouts = sum(1 for item in items if item.error_code == ErrorKind.PROVIDER_TIMEOUT.value)
    rate_limits = sum(1 for item in items if item.error_code == ErrorKind.RATE_LIMITED.value)
    provider_errors = sum(1 for item in items if item.error_code == ErrorKind.PROVIDER_ERROR.value)

    latencies = [item.latency_ms for item in items]
    p50 = percentile(latencies, 0.50)
    p90 = percentile(latencies, 0.90)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    min_lat = round(min(latencies), 3)
    max_lat = round(max(latencies), 3)
    mean_lat = round(sum(latencies) / len(latencies), 3)

    input_tokens = 0
    output_tokens = 0
    total_cost: float = 0.0
    cost_recorded = False

    for item in items:
        if item.usage:
            inp = item.usage.get("input_tokens")
            outp = item.usage.get("output_tokens")
            if inp is not None:
                input_tokens += int(inp)
            if outp is not None:
                output_tokens += int(outp)
            cost = item.usage.get("estimated_cost_usd")
            if cost is not None:
                total_cost += float(cost)
                cost_recorded = True

    return ProviderSummary(
        provider=provider,
        model=model,
        total_requests=total,
        successful_requests=successes,
        failed_requests=failures,
        success_rate=success_rate,
        schema_failures=schema_failures,
        timeouts=timeouts,
        rate_limits=rate_limits,
        provider_errors=provider_errors,
        latency_p50_ms=p50,
        latency_p90_ms=p90,
        latency_p95_ms=p95,
        latency_p99_ms=p99,
        latency_min_ms=min_lat,
        latency_max_ms=max_lat,
        latency_mean_ms=mean_lat,
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        total_cost_usd=round(total_cost, 6) if cost_recorded else None,
    )


async def run_benchmark(
    requests: list[GenerationRequest],
    providers: Sequence[str],
    models: dict[str, str | Sequence[str]] | None = None,
    *,
    service: DirectorService | None = None,
    registry: ProviderRegistry | None = None,
    iterations: int = 1,
    concurrency: int = 1,
    input_name: str = "dataset.jsonl",
    evaluation_id: str | None = None,
    dataset_version: str = CONTRACT_VERSION,
) -> BenchmarkReport:
    """Run replay benchmark for the given requests across selected providers.

    ``models`` maps a provider to one model or to several; every listed model is
    replayed and summarized separately. A provider without an entry uses its default.
    """
    model_map = _resolve_benchmark_selections(
        providers, models, iterations=iterations, concurrency=concurrency
    )
    owned_registry: ProviderRegistry | None = None
    owned_telemetry: DirectorTelemetry | None = None
    if service is None:
        owned_telemetry = _setup_replay_telemetry()
        try:
            service, owned_registry = build_default_service(telemetry=owned_telemetry)
        except BaseException:
            await _shutdown_replay_telemetry(owned_telemetry)
            raise
    reg = registry or owned_registry

    replay_id = f"replay-{uuid.uuid4().hex}"
    dataset_id = (
        "dataset-"
        + hashlib.sha256(
            json.dumps([req.model_dump(mode="json") for req in requests], sort_keys=True).encode()
        ).hexdigest()[:32]
    )

    try:
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(req: GenerationRequest, prov: str, mod: str, it: int) -> ReplayItemResult:
            async with semaphore:
                case_id = (
                    "case-"
                    + hashlib.sha256(req.model_dump_json().encode()).hexdigest()[:32]
                    + f"-{it}"
                )
                with telemetry_context(
                    execution_mode="replay",
                    replay_id=replay_id,
                    evaluation_id=evaluation_id or replay_id,
                    dataset_id=dataset_id,
                    dataset_version=dataset_version,
                    case_id=case_id,
                ):
                    return await run_replay_item(service, req, prov, mod, it)

        tasks: list[asyncio.Task[ReplayItemResult]] = []
        for prov in providers:
            for mod in model_map.get(prov) or ["default"]:
                for it in range(1, iterations + 1):
                    for req in requests:
                        tasks.append(asyncio.create_task(worker(req, prov, mod, it)))

        all_results: list[ReplayItemResult] = await asyncio.gather(*tasks)

        # Group by provider/model
        grouped: dict[tuple[str, str], list[ReplayItemResult]] = defaultdict(list)
        for res in all_results:
            grouped[(res.provider, res.model)].append(res)

        summaries: list[ProviderSummary] = []
        for (prov, mod), items in grouped.items():
            summaries.append(compute_provider_summary(prov, mod, items))

        # Legacy view keyed by provider alone. It cannot hold two models of one
        # provider, so such providers are left out rather than overwritten;
        # `providers` (and benchmarks.summarize) always carry every provider/model.
        models_per_provider: dict[str, int] = defaultdict(int)
        for summary in summaries:
            models_per_provider[summary.provider] += 1
        summary_by_provider = {
            summary.provider: summary.to_dict()
            for summary in summaries
            if models_per_provider[summary.provider] == 1
        }

        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return BenchmarkReport(
            generated_at=now_iso,
            contract_version=CONTRACT_VERSION,
            input_file=input_name,
            total_events_read=len(requests),
            unique_requests=len({r.request_id for r in requests}),
            iterations=iterations,
            concurrency=concurrency,
            providers=summaries,
            summary_by_provider=summary_by_provider,
            results=all_results,
        )
    finally:
        try:
            if reg is not None:
                try:
                    # Drain shadow calls before closing their provider clients.
                    await service.aclose()
                finally:
                    await reg.aclose()
        finally:
            if owned_telemetry is not None:
                await _shutdown_replay_telemetry(owned_telemetry)


def _setup_replay_telemetry() -> DirectorTelemetry:
    try:
        return setup_telemetry(TelemetrySettings.from_env())
    except Exception:
        return DirectorTelemetry(enabled=False)


async def _shutdown_replay_telemetry(telemetry: DirectorTelemetry) -> None:
    # A faulty SDK/exporter must not keep a completed benchmark alive forever.
    def shutdown() -> None:
        try:
            telemetry.shutdown()
        except Exception:
            pass

    thread = threading.Thread(target=shutdown, daemon=True, name="replay-telemetry-shutdown")
    thread.start()
    await asyncio.to_thread(thread.join, 5.0)


def print_human_summary(report: BenchmarkReport) -> None:
    """Print clean terminal summary without polluting stdout for piped JSON."""
    print("=" * 78, file=sys.stderr)
    print(f"DUNGEON DIRECTOR REPLAY BENCHMARK REPORT ({report.generated_at})", file=sys.stderr)
    print(
        f"Input: {report.input_file} | Events: {report.total_events_read} | "
        f"Iterations: {report.iterations} | Concurrency: {report.concurrency}",
        file=sys.stderr,
    )
    print("=" * 78, file=sys.stderr)
    print(
        f"{'Provider':<16} {'Model':<20} {'Success':<10} {'p50 (ms)':<10} "
        f"{'p95 (ms)':<10} {'p99 (ms)':<10} {'Tokens':<8}",
        file=sys.stderr,
    )
    print("-" * 78, file=sys.stderr)
    for p in report.providers:
        succ_str = f"{p.successful_requests}/{p.total_requests} ({p.success_rate * 100:.1f}%)"
        print(
            f"{p.provider:<16} {p.model:<20} {succ_str:<10} {p.latency_p50_ms:<10.1f} "
            f"{p.latency_p95_ms:<10.1f} {p.latency_p99_ms:<10.1f} {p.total_tokens:<8}",
            file=sys.stderr,
        )
    print("=" * 78, file=sys.stderr)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks.replay",
        description="Replay recorded generation datasets against Director providers.",
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to JSON or JSONL file containing recorded generation requests or events.",
    )
    parser.add_argument(
        "--output",
        "-o",
        help=(
            "Path to save machine-readable benchmark report (.json or .jsonl). "
            "If omitted, prints JSON to stdout."
        ),
    )

    parser.add_argument(
        "--providers",
        "-p",
        nargs="+",
        default=[RULES_PROVIDER_ID],
        choices=list(SUPPORTED_PROVIDERS),
        help=f"Providers to evaluate (default: {RULES_PROVIDER_ID}).",
    )
    parser.add_argument(
        "--models",
        "-m",
        nargs="*",
        help=(
            "Custom models in 'provider:model' format (e.g. 'groq:openai/gpt-oss-120b'). "
            "Repeat a provider to replay several of its models."
        ),
    )
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=1,
        help="Number of iterations per request per provider (default: 1).",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=2,
        help="Maximum concurrent in-flight requests (default: 2).",
    )
    parser.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=10.0,
        help="Director timeout in seconds (default: 10.0).",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Do not print human summary table to stderr.",
    )
    parser.add_argument("--evaluation-id", help="Bounded evaluation correlation ID for OpenLIT.")
    parser.add_argument("--dataset-version", default=CONTRACT_VERSION)
    return parser


def parse_model_overrides(model_args: list[str] | None) -> dict[str, str]:
    models: dict[str, str] = {}
    if not model_args:
        return models
    for arg in model_args:
        if ":" in arg:
            prov, mod = arg.split(":", 1)
            models[prov.strip()] = mod.strip()
        else:
            raise ValueError(f"invalid model override format {arg!r}; expected 'provider:model'")
    return models


def parse_model_selections(model_args: list[str] | None) -> dict[str, list[str]]:
    """Parse ``provider:model`` arguments, keeping every model listed for a provider."""
    selections: dict[str, list[str]] = {}
    for arg in model_args or []:
        if ":" not in arg:
            raise ValueError(f"invalid model override format {arg!r}; expected 'provider:model'")
        prov, mod = (part.strip() for part in arg.split(":", 1))
        models = selections.setdefault(prov, [])
        if mod in models:
            raise ValueError(f"duplicate provider/model selection: {prov}/{mod}")
        models.append(mod)
    return selections


async def main_async(args: argparse.Namespace) -> int:
    requests = load_dataset_requests(args.input)
    model_overrides = parse_model_selections(args.models)
    _resolve_benchmark_selections(
        args.providers,
        model_overrides,
        iterations=args.iterations,
        concurrency=args.concurrency,
    )
    if not requests:
        print(f"Warning: no generation requests found in {args.input}", file=sys.stderr)
        return 0

    telemetry = _setup_replay_telemetry()
    try:
        service, registry = build_default_service(timeout_seconds=args.timeout, telemetry=telemetry)
        report = await run_benchmark(
            requests=requests,
            providers=args.providers,
            models=model_overrides,
            service=service,
            registry=registry,
            iterations=args.iterations,
            concurrency=args.concurrency,
            input_name=str(args.input),
            evaluation_id=getattr(args, "evaluation_id", None),
            dataset_version=getattr(args, "dataset_version", CONTRACT_VERSION),
        )
    finally:
        await _shutdown_replay_telemetry(telemetry)

    if not args.quiet:
        print_human_summary(report)

    # Save or print machine-readable output
    report_dict = report.to_dict(include_results=True)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if str(out_path).endswith(".jsonl"):
            with out_path.open("w", encoding="utf-8") as f:
                # Write summary as line 1, then item results
                summary_line = report.to_dict(include_results=False)
                summary_line["type"] = "benchmark_summary"
                f.write(json.dumps(summary_line) + "\n")
                for item in report.results:
                    d = item.to_dict()
                    d["type"] = "benchmark_result"
                    f.write(json.dumps(d) + "\n")
        else:
            out_path.write_text(json.dumps(report_dict, indent=2), encoding="utf-8")
        print(f"Report saved to {out_path}", file=sys.stderr)
    else:
        print(json.dumps(report_dict, indent=2))

    return 0


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        exit_code = asyncio.run(main_async(args))
        sys.exit(exit_code)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
