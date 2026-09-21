"""Tests for the replay benchmark engine and CLI (issue #14).

Tests exercise:
- Dataset loading (JSONL and JSON list formats, handling request envelopes).
- Replay execution against rules-baseline, cloudflare-jev, groq, and cerebras.
- Mock/fake transport injection for offline-safe execution of all hosted providers.
- Percentile calculation and latency/failure telemetry aggregation.
- CLI argument parsing, file outputs (.json and .jsonl), and error reporting.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.replay import (
    CEREBRAS_PROVIDER_ID,
    CLOUDFLARE_JEV_PROVIDER_ID,
    GROQ_PROVIDER_ID,
    RULES_PROVIDER_ID,
    BenchmarkReport,
    build_arg_parser,
    build_default_service,
    compute_provider_summary,
    load_dataset_requests,
    main_async,
    parse_model_overrides,
    percentile,
    run_benchmark,
    run_replay_item,
)
from fakes import make_request

from dungeon_director.cerebras import (
    CerebrasConfig,
    CerebrasProvider,
    CerebrasTransportRequest,
    CerebrasTransportResponse,
)
from dungeon_director.cloudflare_jev import (
    CloudflareJevProvider,
    JevConfig,
    JevTransportRequest,
    JevTransportResponse,
)
from dungeon_director.contracts import ErrorKind
from dungeon_director.groq import (
    GroqConfig,
    GroqProvider,
    GroqTransportRequest,
    GroqTransportResponse,
)
from dungeon_director.registry import ProviderRegistry
from dungeon_director.rules import RULES_MODEL, RulesProvider
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks" / "fixtures"


class FakeJevTransport:
    def __init__(self, response_body: dict[str, Any] | None = None, status: int = 200) -> None:
        self.requests: list[JevTransportRequest] = []
        self._status = status
        if response_body is not None:
            self._body = json.dumps(response_body).encode("utf-8")
        else:
            canned = {
                "result": {
                    "model": "typesafe/jev",
                    "answers": {
                        "room_type": {
                            "type": "choice",
                            "choice": "chamber",
                            "confidence": 0.9,
                            "probabilities": {"chamber": 0.8},
                        },
                        "size": {
                            "type": "choice",
                            "choice": "medium",
                            "confidence": 0.9,
                            "probabilities": {"medium": 0.9},
                        },
                        "danger": {
                            "type": "score",
                            "score": 1.0,
                            "confidence": 0.9,
                            "probabilities": {"1": 0.9},
                        },
                        "enemy_density": {
                            "type": "score",
                            "score": 0.5,
                            "confidence": 0.9,
                            "probabilities": {"0": 0.5, "1": 0.5},
                        },
                        "loot_density": {
                            "type": "score",
                            "score": 1.0,
                            "confidence": 0.9,
                            "probabilities": {"1": 0.9},
                        },
                        "has_secret": {"type": "noul", "noul": 0.1},
                        "exit_count": {
                            "type": "choice",
                            "choice": "1",
                            "confidence": 0.9,
                            "probabilities": {"1": 0.9},
                        },
                        "atmosphere": {
                            "type": "choice",
                            "choice": "dark",
                            "confidence": 0.7,
                            "probabilities": {"dark": 0.7, "none": 0.3},
                        },
                    },
                    "usage": {"input_tokens": 120, "output_tokens": 45},
                },
                "success": True,
                "errors": [],
                "messages": [],
            }
            self._body = json.dumps(canned).encode("utf-8")

    async def send(self, request: JevTransportRequest) -> JevTransportResponse:
        self.requests.append(request)
        return JevTransportResponse(status_code=self._status, headers={}, body=self._body)


class FakeGroqTransport:
    def __init__(self, room_plan: dict[str, Any] | None = None, status: int = 200) -> None:
        self.requests: list[GroqTransportRequest] = []
        self._status = status
        plan = room_plan or {
            "room_id": "groq-test-room",
            "depth": 3,
            "room_type": "room",
            "size": "small",
            "danger": 1,
            "exits": [{"direction": "south", "kind": "door", "locked": False}],
            "enemy_density": 0.1,
            "loot_density": 0.2,
            "secret_probability": 0.0,
            "has_secret": False,
            "environmental_tags": ["dark"],
        }
        resp = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(plan)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250},
        }
        self._body = json.dumps(resp).encode("utf-8")

    async def send(self, request: GroqTransportRequest) -> GroqTransportResponse:
        self.requests.append(request)
        return GroqTransportResponse(status_code=self._status, headers={}, body=self._body)


class FakeCerebrasTransport:
    def __init__(self, room_plan: dict[str, Any] | None = None, status: int = 200) -> None:
        self.requests: list[CerebrasTransportRequest] = []
        self._status = status
        plan = room_plan or {
            "room_id": "cerebras-test-room",
            "depth": 3,
            "room_type": "chamber",
            "size": "small",
            "danger": 1,
            "exits": [{"direction": "south", "kind": "door", "locked": False}],
            "enemy_density": 0.0,
            "loot_density": 0.3,
            "secret_probability": 0.0,
            "has_secret": False,
            "environmental_tags": [],
        }

        resp = {
            "id": "chatcmpl-cerebras",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(plan)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 180, "completion_tokens": 40, "total_tokens": 220},
        }
        self._body = json.dumps(resp).encode("utf-8")

    async def send(self, request: CerebrasTransportRequest) -> CerebrasTransportResponse:
        self.requests.append(request)
        return CerebrasTransportResponse(status_code=self._status, headers={}, body=self._body)


def test_percentile_calculation():
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(vals, 0.50) == 30.0
    assert percentile(vals, 0.0) == 10.0
    assert percentile(vals, 1.0) == 50.0
    assert percentile([], 0.50) == 0.0
    assert percentile([42.0], 0.90) == 42.0


def test_load_dataset_requests_jsonl(tmp_path: Path):
    fixture_path = FIXTURES_DIR / "sample_run.jsonl"
    assert fixture_path.is_file(), f"{fixture_path} should exist"

    requests = load_dataset_requests(fixture_path)
    assert len(requests) == 3
    assert requests[0].request_id == "req-run-fixture-001-1"
    assert requests[1].request_id == "req-run-fixture-001-2"
    assert requests[2].request_id == "req-run-fixture-001-3"


def test_load_dataset_requests_json_array(tmp_path: Path):
    req = make_request()
    json_path = tmp_path / "requests.json"
    json_path.write_text(json.dumps([req.model_dump(mode="json")]), encoding="utf-8")

    loaded = load_dataset_requests(json_path)
    assert len(loaded) == 1
    assert loaded[0].request_id == req.request_id


def test_load_dataset_nonexistent_file():
    with pytest.raises(FileNotFoundError):
        load_dataset_requests("/nonexistent/file.jsonl")


def test_load_dataset_malformed_json(tmp_path: Path):
    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        load_dataset_requests(bad_path)


def test_replay_item_rules_baseline():
    service, _ = build_default_service()
    req = make_request()
    res = asyncio.run(run_replay_item(service, req, RULES_PROVIDER_ID, "builtin-v1", 1))

    assert res.success is True
    assert res.status_code == 200
    assert res.room is not None
    assert res.room["depth"] == req.state.depth
    assert res.error_code is None
    assert res.latency_ms > 0.0


def test_replay_all_four_providers_offline():
    jev_transport = FakeJevTransport()
    groq_transport = FakeGroqTransport()
    cerebras_transport = FakeCerebrasTransport()

    registry = ProviderRegistry()
    registry.register(RulesProvider())
    registry.register(
        CloudflareJevProvider(
            JevConfig(account_id="fake-acct", api_token="fake-token"),
            transport=jev_transport,
        )
    )
    registry.register(GroqProvider(GroqConfig(api_key="fake-groq-key"), transport=groq_transport))
    registry.register(
        CerebrasProvider(CerebrasConfig(api_key="fake-cerebras-key"), transport=cerebras_transport)
    )

    settings = DirectorSettings(default_provider=RULES_PROVIDER_ID)
    service = DirectorService(registry, settings)

    req = make_request()
    providers = [
        RULES_PROVIDER_ID,
        CLOUDFLARE_JEV_PROVIDER_ID,
        GROQ_PROVIDER_ID,
        CEREBRAS_PROVIDER_ID,
    ]

    report: BenchmarkReport = asyncio.run(
        run_benchmark(
            requests=[req],
            providers=providers,
            service=service,
            iterations=1,
            concurrency=2,
            input_name="sample_test",
        )
    )

    assert report.total_events_read == 1
    assert len(report.providers) == 4
    for summary in report.providers:
        assert summary.successful_requests == 1
        assert summary.success_rate == 1.0
        assert summary.failed_requests == 0
        assert summary.latency_p50_ms > 0.0

    # Ensure transports received the calls
    assert len(jev_transport.requests) == 1
    assert len(groq_transport.requests) == 1
    assert len(cerebras_transport.requests) == 1


def test_replay_failure_classification():
    # Groq returning schema violation (e.g. missing depth)
    bad_room = {
        "room_id": "bad-room",
        "room_type": "room",
        "size": "small",
    }
    groq_transport = FakeGroqTransport(room_plan=bad_room)

    registry = ProviderRegistry()
    registry.register(RulesProvider())
    registry.register(GroqProvider(GroqConfig(api_key="fake-key"), transport=groq_transport))
    settings = DirectorSettings(default_provider=RULES_PROVIDER_ID)
    service = DirectorService(registry, settings)

    req = make_request()
    res = asyncio.run(run_replay_item(service, req, GROQ_PROVIDER_ID, "openai/gpt-oss-20b", 1))

    assert res.success is False
    assert res.status_code == 502
    assert res.error_code == ErrorKind.SCHEMA_VIOLATION.value
    assert res.error_message is not None

    summary = compute_provider_summary(GROQ_PROVIDER_ID, "openai/gpt-oss-20b", [res])
    assert summary.total_requests == 1
    assert summary.failed_requests == 1
    assert summary.schema_failures == 1
    assert summary.success_rate == 0.0


def test_arg_parser_and_model_overrides():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--input",
            "benchmarks/fixtures/sample_run.jsonl",
            "--providers",
            "rules-baseline",
            "groq",
            "--models",
            "groq:openai/gpt-oss-120b",
            "--iterations",
            "2",
            "--concurrency",
            "4",
        ]
    )
    assert args.input == "benchmarks/fixtures/sample_run.jsonl"
    assert args.providers == ["rules-baseline", "groq"]
    assert args.iterations == 2
    assert args.concurrency == 4

    overrides = parse_model_overrides(args.models)
    assert overrides == {"groq": "openai/gpt-oss-120b"}

    with pytest.raises(ValueError, match="invalid model override format"):
        parse_model_overrides(["invalid_format_no_colon"])


@pytest.mark.parametrize(
    ("providers", "models", "iterations", "concurrency", "expected"),
    [
        ([RULES_PROVIDER_ID], None, 0, 1, "iterations must be a positive integer"),
        ([RULES_PROVIDER_ID], None, 1, 0, "concurrency must be a positive integer"),
        (
            [RULES_PROVIDER_ID, RULES_PROVIDER_ID],
            None,
            1,
            1,
            "duplicate provider/model selection",
        ),
        (
            [GROQ_PROVIDER_ID],
            {GROQ_PROVIDER_ID: ["model-a", "model-a"]},
            1,
            1,
            "duplicate provider/model selection",
        ),
    ],
)
def test_run_benchmark_rejects_invalid_definitions_before_service_construction(
    monkeypatch, providers, models, iterations, concurrency, expected
):
    import benchmarks.replay as replay_module

    def unexpected_service_construction(*args, **kwargs):
        raise AssertionError("validation must run before service construction")

    monkeypatch.setattr(replay_module, "build_default_service", unexpected_service_construction)
    with pytest.raises(ValueError, match=expected):
        asyncio.run(
            run_benchmark(
                [make_request()],
                providers=providers,
                models=models,
                iterations=iterations,
                concurrency=concurrency,
            )
        )


def test_main_async_rejects_invalid_cardinality_even_for_an_empty_dataset(tmp_path: Path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    args = build_arg_parser().parse_args(["--input", str(empty), "--iterations", "0", "--quiet"])

    with pytest.raises(ValueError, match="iterations must be a positive integer"):
        asyncio.run(main_async(args))


def test_cli_execution_with_output_files(tmp_path: Path):
    fixture_path = FIXTURES_DIR / "sample_run.jsonl"
    json_out = tmp_path / "report.json"
    jsonl_out = tmp_path / "report.jsonl"

    parser = build_arg_parser()

    # Test JSON output
    args = parser.parse_args(["--input", str(fixture_path), "--output", str(json_out), "--quiet"])
    code = asyncio.run(main_async(args))
    assert code == 0
    assert json_out.is_file()
    data = json.loads(json_out.read_text(encoding="utf-8"))
    assert "contract_version" in data
    assert "providers" in data
    assert "results" in data
    assert len(data["results"]) == 3

    # Test JSONL output
    args = parser.parse_args(["--input", str(fixture_path), "--output", str(jsonl_out), "--quiet"])
    code = asyncio.run(main_async(args))
    assert code == 0
    assert jsonl_out.is_file()
    lines = jsonl_out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4  # 1 summary line + 3 result lines
    summary = json.loads(lines[0])
    assert summary.get("type") == "benchmark_summary"
    first_res = json.loads(lines[1])
    assert first_res.get("type") == "benchmark_result"
    assert first_res.get("request_id") == "req-run-fixture-001-1"


class CloseTrackingProvider(RulesProvider):
    def __init__(self, provider_id: str = "close-tracker") -> None:
        self.provider_id = provider_id
        self.models = (RULES_MODEL,)
        self.default_model = RULES_MODEL
        self.closed = False
        self.close_calls = 0

    async def aclose(self) -> None:
        self.closed = True
        self.close_calls += 1


def test_run_benchmark_lifecycle_closes_service_and_registry_on_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    req = make_request()

    # Success case: provider closes cleanly
    tracker_success = CloseTrackingProvider("tracker-success")
    reg_success = ProviderRegistry()
    reg_success.register(tracker_success)
    service_success = DirectorService(
        reg_success,
        DirectorSettings(default_provider="tracker-success", default_model="builtin-v1"),
    )
    success_service_closes: list[int] = []
    original_success_close = service_success.aclose

    async def close_success_service() -> None:
        success_service_closes.append(1)
        await original_success_close()

    monkeypatch.setattr(service_success, "aclose", close_success_service)

    report = asyncio.run(
        run_benchmark(
            [req],
            providers=["tracker-success"],
            models={"tracker-success": "builtin-v1"},
            service=service_success,
            registry=reg_success,
        )
    )
    assert report.total_events_read == 1
    assert tracker_success.closed is True
    assert tracker_success.close_calls == 1
    assert success_service_closes == [1]

    # Failure case: provider closes even if exception occurs during run
    tracker_failure = CloseTrackingProvider("tracker-fail")
    reg_failure = ProviderRegistry()
    reg_failure.register(tracker_failure)
    service_failure = DirectorService(
        reg_failure,
        DirectorSettings(default_provider="tracker-fail", default_model="builtin-v1"),
    )
    failure_service_closes: list[int] = []
    original_failure_close = service_failure.aclose

    async def close_failure_service() -> None:
        failure_service_closes.append(1)
        await original_failure_close()

    monkeypatch.setattr(service_failure, "aclose", close_failure_service)

    class CustomError(Exception):
        pass

    async def exploding_worker(*args: Any, **kwargs: Any) -> Any:
        raise CustomError("benchmark exploded")

    import benchmarks.replay as replay_module

    orig_run_item = replay_module.run_replay_item
    replay_module.run_replay_item = exploding_worker  # type: ignore[assignment]
    try:
        with pytest.raises(CustomError):
            asyncio.run(
                run_benchmark(
                    [req],
                    providers=["tracker-fail"],
                    models={"tracker-fail": "builtin-v1"},
                    service=service_failure,
                    registry=reg_failure,
                )
            )
    finally:
        replay_module.run_replay_item = orig_run_item

    assert tracker_failure.closed is True
    assert tracker_failure.close_calls == 1
    assert failure_service_closes == [1]
