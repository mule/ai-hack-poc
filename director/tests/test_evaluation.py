"""Tests for the POC evaluation protocol (issue #18).

Everything here is offline: hosted providers are replaced by fakes, and the
committed replay corpus is the only input.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from benchmarks.evaluation.behavior import pairwise_agreement, selection_behavior
from benchmarks.evaluation.bundle import Bundle, plan_digest, verify_bundle
from benchmarks.evaluation.corpus import (
    MANIFEST_NAME,
    REQUESTS_NAME,
    CorpusError,
    build_corpus,
    canonical_json,
    load_corpus,
    verify_corpus,
)
from benchmarks.evaluation.cost import PricingError, derive_cost, load_pricing
from benchmarks.evaluation.environment import (
    Selection,
    SelectionError,
    _safe_url,
    check_usable,
    describe_config,
    resolve_selection,
)
from benchmarks.evaluation.protocol import (
    ProtocolError,
    load_protocol,
    min_samples_for,
    resolve_parameters,
)
from benchmarks.evaluation.report import AnalysisError, analyze, load_observations, run_summarizer
from benchmarks.evaluation.runner import (
    EvaluationError,
    compute_offline_plan_digest,
    run_evaluation,
)
from fakes import FakeProvider, valid_room_dict

from dungeon_director.contracts import UsageStats
from dungeon_director.groq import GroqConfig
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.rules import RulesProvider
from dungeon_director.settings import DirectorSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "benchmarks" / "corpus" / "replay-v1" / MANIFEST_NAME
SECRET = "gsk_TOPSECRETKEY123456"


class PricedFake(FakeProvider):
    """A hosted-looking fake that reports tokens and upstream identity."""

    async def generate(self, request, *, model):
        self.calls += 1
        return ProviderResult(
            payload=valid_room_dict(request),
            usage=UsageStats(input_tokens=200, output_tokens=50),
            provider_metadata={"system_fingerprint": "fp-1", "note": "ignored"},
        )


def fake_factory(selection: Selection, timeout: float):
    from dungeon_director.service import DirectorService

    registry = ProviderRegistry()
    registry.register(RulesProvider())
    if selection.is_live:
        registry.register(PricedFake(selection.provider, models=(selection.model,)))
    settings = DirectorSettings(timeout_seconds=timeout)
    return DirectorService(registry, settings), registry


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(CORPUS)


@pytest.fixture(scope="module")
def offline_bundle(tmp_path_factory, corpus):
    out = tmp_path_factory.mktemp("bundle") / "b"
    params = resolve_parameters(load_protocol(), "smoke", iterations=2)
    run_evaluation(
        corpus,
        [resolve_selection("rules-baseline", {})],
        params,
        out,
        repo_root=REPO_ROOT,
        progress=lambda _: None,
    )
    analyze(out, repo_root=REPO_ROOT)
    return out


@pytest.fixture
def bundle_copy(offline_bundle, tmp_path):
    target = tmp_path / "b"
    shutil.copytree(offline_bundle, target)
    return target


# --- corpus -----------------------------------------------------------------


def test_committed_corpus_verifies_and_is_replay_kind(corpus):
    assert corpus.kind == "replay"
    assert len(corpus.requests) == 150
    assert corpus.manifest["provenance"]["simulation_args"] == "--runs 5 --steps 30 --seed 100"


def test_committed_corpus_reproduces_the_expected_offline_plans(corpus):
    golden = corpus.manifest["expected_offline"]["rules-baseline/builtin-v1"]["plan_digest"]
    assert compute_offline_plan_digest(corpus.requests) == golden


def _copy_corpus(tmp_path: Path) -> Path:
    target = tmp_path / "corpus"
    shutil.copytree(CORPUS.parent, target)
    return target


def test_corpus_detects_a_changed_request_file(tmp_path):
    target = _copy_corpus(tmp_path)
    path = target / REQUESTS_NAME
    path.write_text(path.read_text().replace('"hp":20', '"hp":19', 1))
    problems, _ = verify_corpus(target / MANIFEST_NAME)
    assert any("sha256" in p for p in problems)


def test_corpus_rejects_non_canonical_and_duplicate_lines(tmp_path):
    target = _copy_corpus(tmp_path)
    lines = (target / REQUESTS_NAME).read_text().splitlines()
    manifest = json.loads((target / MANIFEST_NAME).read_text())
    spaced = json.dumps(json.loads(lines[0]), indent=1).replace("\n", "")
    body = f"{spaced}\n{lines[0]}\n"
    (target / REQUESTS_NAME).write_text(body)
    manifest["files"][0].update(
        sha256=__import__("hashlib").sha256(body.encode()).hexdigest(),
        bytes=len(body.encode()),
        records=2,
    )
    (target / MANIFEST_NAME).write_text(json.dumps(manifest))
    problems, _ = verify_corpus(target / MANIFEST_NAME)
    assert any("canonical" in p for p in problems)
    assert any("duplicate request_id" in p for p in problems)


def test_corpus_rejects_path_traversal_and_unknown_kind(tmp_path):
    target = _copy_corpus(tmp_path)
    manifest = json.loads((target / MANIFEST_NAME).read_text())
    manifest["files"][0]["path"] = "../requests.jsonl"
    manifest["kind"] = "synthetic"
    (target / MANIFEST_NAME).write_text(json.dumps(manifest))
    problems, _ = verify_corpus(target / MANIFEST_NAME)
    assert problems and any("exactly one entry" in p for p in problems)
    with pytest.raises(CorpusError):
        load_corpus(target / MANIFEST_NAME)


def test_build_corpus_from_a_recording_is_canonical_and_deterministic(tmp_path):
    lines = (CORPUS.parent / REQUESTS_NAME).read_text().splitlines()[:5]
    recording = tmp_path / "rec.jsonl"
    recording.write_text(
        "".join(
            json.dumps({"request": json.loads(x), "provider": "p", "model": "m", "outcome": "ok"})
            + "\n"
            for x in lines
        )
    )
    a = build_corpus(recording, tmp_path / "a", corpus_id="t", kind="live", description="d")
    b = build_corpus(recording, tmp_path / "b", corpus_id="t", kind="live", description="d")
    assert a == b
    assert (tmp_path / "a" / REQUESTS_NAME).read_text() == "".join(x + "\n" for x in lines)
    assert a["kind"] == "live"
    assert a["provenance"]["recorded_providers"] == {"p/m": 5}
    with pytest.raises(CorpusError):
        build_corpus(recording, tmp_path / "c", corpus_id="t", kind="bogus", description="d")


# --- protocol ---------------------------------------------------------------


def test_sample_size_rule_puts_ten_observations_above_the_percentile():
    tail = load_protocol()["min_tail_samples"]
    assert [min_samples_for(p, tail) for p in (0.5, 0.9, 0.95, 0.99)] == [20, 100, 200, 1000]


def test_full_tier_supports_p99_on_the_committed_corpus(corpus):
    protocol = load_protocol()
    params = resolve_parameters(protocol, "full")
    assert params["iterations"] * len(corpus.requests) >= min_samples_for(0.99, 10)
    assert params["overrides"] == {}


def test_overrides_are_recorded_and_bad_input_rejected():
    params = resolve_parameters(load_protocol(), "smoke", iterations=4, timeout_seconds=2.5)
    assert params["overrides"] == {"iterations": 4, "timeout_seconds": 2.5}
    with pytest.raises(ProtocolError):
        resolve_parameters(load_protocol(), "huge")
    with pytest.raises(ProtocolError):
        resolve_parameters(load_protocol(), "smoke", iterations=0)


# --- environment and configuration capture -----------------------------------


def test_config_capture_withholds_credentials_but_keeps_tuning():
    config = GroqConfig(api_key=SECRET)
    described = describe_config(config)
    assert "api_key" in described["withheld"]
    assert described["credentials_present"] is True
    assert SECRET not in json.dumps(described)
    assert described["fields"]["max_completion_tokens"] == config.max_completion_tokens
    assert described["fields"]["api_base_url"] == "https://api.groq.com"
    # Capture strips credentials, queries, fragments, and potentially secret paths.
    assert _safe_url("https://user:pw@host.example:8443/tenant/key?x=1#f") == (
        "https://host.example:8443"
    )


def test_selection_uses_environment_model_and_explicit_override():
    env = {"GROQ_API_KEY": SECRET, "GROQ_MODEL": "openai/gpt-oss-120b"}
    assert resolve_selection("groq", env).model == "openai/gpt-oss-120b"
    explicit = resolve_selection("groq:openai/gpt-oss-20b", env)
    assert explicit.model == "openai/gpt-oss-20b"
    assert explicit.config.model == "openai/gpt-oss-20b"
    with pytest.raises(SelectionError):
        resolve_selection("openai", {})
    with pytest.raises(SelectionError):
        resolve_selection("groq", {"GROQ_MAX_COMPLETION_TOKENS": "many"})


def test_unconfigured_hosted_provider_is_not_usable():
    with pytest.raises(SelectionError, match="not usable"):
        check_usable(resolve_selection("groq", {}))
    check_usable(resolve_selection("groq", {"GROQ_API_KEY": SECRET}))


# --- runner -----------------------------------------------------------------


def test_offline_bundle_has_evidence_and_verifies(offline_bundle):
    assert verify_bundle(offline_bundle) == []
    for name in (
        "manifest.json", "environment.json", "protocol.json", "results.json", "warmup.jsonl",
        "metrics.json", "behavior.json", "report.md", "corpus/corpus.json",
    ):  # fmt: skip
        assert (offline_bundle / name).is_file(), name
    env = json.loads((offline_bundle / "environment.json").read_text())
    assert env["runtime"]["packages"]["pydantic"]
    assert env["contract_version"] == "1.0.0"
    manifest = json.loads((offline_bundle / "manifest.json").read_text())
    assert manifest["run"]["live"] is False
    assert manifest["corpus"]["requests_sha256"] == load_corpus(CORPUS).requests_sha256


def test_warmup_is_separate_from_measured_results(offline_bundle):
    bundle = Bundle(offline_bundle)
    assert bundle.warmup and all(r["phase"] == "warmup" for r in bundle.warmup)
    assert all(r["phase"] == "measured" for r in bundle.results)
    assert len(bundle.results) == 2 * 150
    # rows keep the replay-result shape the issue #15 summarizer reads
    assert {"provider", "model", "request_id", "success", "latency_ms", "retry_count"} <= set(
        bundle.results[0]
    )
    assert bundle.results[0]["retry_count"] is None


def test_order_is_paired_seeded_and_reproducible(offline_bundle, corpus, tmp_path):
    out = tmp_path / "again"
    run_evaluation(
        corpus,
        [resolve_selection("rules-baseline", {})],
        resolve_parameters(load_protocol(), "smoke", iterations=2),
        out,
        repo_root=REPO_ROOT,
        progress=lambda _: None,
    )

    def order(path):
        return [r["request_id"] for r in Bundle(path).results]

    assert order(out) == order(offline_bundle)
    first = order(offline_bundle)[:150]
    assert first != [r.request_id for r in corpus.requests]  # a shuffle, not file order


def test_hosted_run_is_paired_rotated_and_never_leaks_secrets(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    selections = [
        resolve_selection("rules-baseline", {}),
        resolve_selection("groq", {"GROQ_API_KEY": SECRET}),
    ]
    out = tmp_path / "live"
    params = resolve_parameters(load_protocol(), "smoke", iterations=2, warmup_requests=2)
    run_evaluation(
        corpus, selections, params, out, repo_root=REPO_ROOT, live=True,
        service_factory=fake_factory, progress=lambda _: None,
    )  # fmt: skip
    analyze(out, repo_root=REPO_ROOT)
    bundle = Bundle(out)
    rows = bundle.results
    # paired: the two selections answer the same request back to back ...
    assert [r["request_id"] for r in rows[0::2]] == [r["request_id"] for r in rows[1::2]]
    # ... and the provider that goes first alternates
    assert {r["provider"] for r in rows[0::2]} == {"rules-baseline", "groq"}
    text = "".join(p.read_text() for p in out.rglob("*") if p.is_file() and p.suffix != ".jsonl")
    assert SECRET not in text
    env = json.loads((out / "environment.json").read_text())
    groq = next(p for p in env["providers"] if p["provider"] == "groq")
    assert groq["config"]["credentials_present"] is True and groq["live"] is True
    metrics = json.loads((out / "metrics.json").read_text())
    identity = metrics["groq/openai/gpt-oss-20b"]["observed_upstream_identity"]
    assert identity == {"system_fingerprint": {"fp-1": 2 * 150 + 2}}  # measured + warm-up rows


def test_live_run_needs_explicit_confirmation_and_respects_the_budget(corpus, tmp_path):
    hosted = [resolve_selection("groq", {"GROQ_API_KEY": SECRET})]
    params = resolve_parameters(load_protocol(), "smoke")
    with pytest.raises(EvaluationError, match="--live"):
        run_evaluation(corpus, hosted, params, tmp_path / "a", repo_root=REPO_ROOT,
                       service_factory=fake_factory, progress=lambda _: None)  # fmt: skip
    with pytest.raises(EvaluationError, match="max-live-calls"):
        run_evaluation(
            corpus, hosted, params, tmp_path / "b", repo_root=REPO_ROOT, live=True,
            max_live_calls=10, service_factory=fake_factory, progress=lambda _: None,
        )  # fmt: skip
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_run_refuses_a_used_output_directory_and_duplicate_selection(corpus, tmp_path):
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "x").write_text("x")
    params = resolve_parameters(load_protocol(), "smoke")
    rules = resolve_selection("rules-baseline", {})
    with pytest.raises(EvaluationError, match="already holds"):
        run_evaluation(corpus, [rules], params, tmp_path / "used", repo_root=REPO_ROOT)
    with pytest.raises(EvaluationError, match="twice"):
        run_evaluation(corpus, [rules, rules], params, tmp_path / "n", repo_root=REPO_ROOT)


# --- verification: results must come from a run of this corpus ---------------


def test_verify_detects_edited_results(bundle_copy):
    results = bundle_copy / "results.json"
    doc = json.loads(results.read_text())
    doc["results"][0]["latency_ms"] = 0.001
    results.write_text(json.dumps(doc))
    assert any("results.json: sha256" in p for p in verify_bundle(bundle_copy))


def test_synthetic_results_cannot_pass_as_measured(bundle_copy):
    """Fixture-style results (other request ids, other counts) never verify."""
    synthetic = {
        "contract_version": "1.0.0",
        "results": [
            {
                "provider": "groq",
                "model": "openai/gpt-oss-20b",
                "request_id": f"req-{i}",
                "iteration": 1,
                "success": True,
                "latency_ms": 100.0 + i,
            }
            for i in range(50)
        ],
    }
    (bundle_copy / "results.json").write_text(json.dumps(synthetic))
    problems = verify_bundle(bundle_copy)
    assert problems
    with pytest.raises(AnalysisError, match="not a trustworthy"):
        analyze(bundle_copy, repo_root=REPO_ROOT)
    # even with the manifest digest rewritten to match, coverage and identity still fail
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from benchmarks.evaluation.corpus import sha256_file

    manifest["raw_files"]["results.json"]["sha256"] = sha256_file(bundle_copy / "results.json")
    manifest_path.write_text(json.dumps(manifest))
    problems = verify_bundle(bundle_copy)
    assert any("do not cover the corpus" in p or "measured rows" in p for p in problems)
    assert any("unselected provider/model" in p for p in problems)


def test_verify_detects_a_swapped_corpus_and_missing_files(bundle_copy):
    (bundle_copy / "corpus" / REQUESTS_NAME).write_text("")
    assert verify_bundle(bundle_copy)
    (bundle_copy / "warmup.jsonl").unlink()
    assert verify_bundle(bundle_copy)
    assert verify_bundle(bundle_copy / "nowhere")


def test_verify_detects_a_plan_digest_that_does_not_reproduce(bundle_copy):
    results = bundle_copy / "results.json"
    doc = json.loads(results.read_text())
    victim = next(r for r in doc["results"] if r["iteration"] == 1)
    victim["room"]["danger"] = 5
    results.write_text(json.dumps(doc))
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from benchmarks.evaluation.corpus import sha256_file

    manifest["raw_files"]["results.json"]["sha256"] = sha256_file(results)
    manifest_path.write_text(json.dumps(manifest))
    assert any("expected plan digest" in p for p in verify_bundle(bundle_copy))


@pytest.mark.parametrize(
    ("file_name", "replacement", "expected"),
    [
        ("manifest.json", [], "JSON object"),
        ("results.json", {"results": ["not-a-row"]}, "list of objects"),
        ("warmup.jsonl", "[]\n", "JSON object"),
    ],
)
def test_verify_rejects_malformed_bundle_shapes_without_a_traceback(
    bundle_copy, file_name, replacement, expected
):
    path = bundle_copy / file_name
    path.write_text(replacement if isinstance(replacement, str) else json.dumps(replacement))
    problems = verify_bundle(bundle_copy)
    assert problems and any(expected in problem for problem in problems)


def test_verify_rejects_a_malformed_raw_file_entry_without_a_traceback(bundle_copy):
    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["raw_files"]["results.json"] = {}
    manifest_path.write_text(json.dumps(manifest))
    problems = verify_bundle(bundle_copy)
    assert any("manifest sha256" in problem for problem in problems)


def test_verify_enforces_cross_file_identity_warmup_and_seeded_order(bundle_copy):
    protocol_path = bundle_copy / "protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["selections"][0]["model"] = "different-model"
    protocol_path.write_text(json.dumps(protocol))

    warmup_path = bundle_copy / "warmup.jsonl"
    warmup = [json.loads(line) for line in warmup_path.read_text().splitlines()]
    warmup[0]["request_id"], warmup[1]["request_id"] = (
        warmup[1]["request_id"],
        warmup[0]["request_id"],
    )
    warmup_path.write_text("".join(canonical_json(row) + "\n" for row in warmup))

    manifest_path = bundle_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    from benchmarks.evaluation.corpus import sha256_file

    for name in ("protocol.json", "warmup.jsonl"):
        manifest["raw_files"][name]["sha256"] = sha256_file(bundle_copy / name)
        manifest["raw_files"][name]["bytes"] = (bundle_copy / name).stat().st_size
    manifest_path.write_text(json.dumps(manifest))

    problems = verify_bundle(bundle_copy)
    assert any("protocol selections" in problem for problem in problems)
    assert any("seeded warm-up" in problem for problem in problems)


# --- cost -------------------------------------------------------------------


def _row(ok=True, tin=1000, tout=500):
    usage = None if tin is None else {"input_tokens": tin, "output_tokens": tout}
    return {"success": ok, "usage": usage}


def test_cost_needs_a_price_and_never_defaults_to_zero():
    assert derive_cost([_row()], None)["status"] == "no_price"
    unpriced = {"input_usd_per_1m_tokens": None, "output_usd_per_1m_tokens": None}
    assert derive_cost([_row()], unpriced)["status"] == "no_price"


def test_cost_normalizes_per_1000_decisions_and_charges_failures():
    entry = {"input_usd_per_1m_tokens": 1.0, "output_usd_per_1m_tokens": 2.0, "source": "s",
             "retrieved_on": "2026-01-01"}  # fmt: skip
    cost = derive_cost([_row(True), _row(False)], entry)
    # 1000 in + 500 out = 0.001 + 0.001 = 0.002 per call; two calls charged
    assert cost["total_usd"] == pytest.approx(0.004)
    assert cost["per_1000_decisions_usd"] == pytest.approx(2.0)
    assert cost["per_1000_successful_decisions_usd"] == pytest.approx(4.0)
    assert cost["status"] == "computed"


def test_cost_flags_partial_token_coverage_and_flat_per_request_price():
    entry = {"input_usd_per_1m_tokens": 1.0, "output_usd_per_1m_tokens": 1.0, "source": "s",
             "retrieved_on": "d"}  # fmt: skip
    partial = derive_cost([_row(), _row(tin=None)], entry)
    assert partial["status"] == "partial_usage" and partial["requests_costed"] == 1
    per_request = {"per_request_usd": 0.01, "source": "s", "retrieved_on": "d"}
    flat = derive_cost([_row(tin=None)], per_request)
    assert flat["per_1000_decisions_usd"] == pytest.approx(10.0)
    assert derive_cost([_row(tin=None)], entry)["status"] == "no_usage"


def test_pricing_rejects_unsourced_prices_and_the_template_has_none(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"prices": [{"provider": "a", "model": "b", "per_request_usd": 1}]}))
    with pytest.raises(PricingError, match="source"):
        load_pricing(bad)
    bad.write_text(json.dumps({"prices": [{"provider": "a", "model": "b", "per_request_usd": -1,
                                           "source": "s", "retrieved_on": "d"}]}))  # fmt: skip
    with pytest.raises(PricingError):
        load_pricing(bad)
    template = load_pricing()
    for key, entry in template.items():
        if key[0] != "rules-baseline":
            assert entry["input_usd_per_1m_tokens"] is None and entry["source"] is None


# --- behaviour --------------------------------------------------------------


def _decision(rid, room_type="room", danger=1, it=1, ok=True):
    room = {"room_type": room_type, "size": "small", "danger": danger,
            "exits": [{"direction": "north"}]}  # fmt: skip
    return {"request_id": rid, "iteration": it, "success": ok, "room": room if ok else None}


def test_behavior_measures_repetition_diversity_and_danger_progression():
    order = [("run", f"r{i}") for i in range(6)]
    plan = [("room", 1), ("room", 1), ("corridor", 2), ("room", 3), ("room", 4), ("room", 5)]
    rows = [_decision(f"r{i}", kind, danger) for i, (kind, danger) in enumerate(plan)]
    b = selection_behavior(rows, order)
    assert b["repetition"]["consecutive_same_room_type_rate"] == pytest.approx(0.6)
    assert b["repetition"]["longest_same_room_type_streak"] == 3
    assert b["danger_progression"]["first_third_mean"] == 1.0
    assert b["danger_progression"]["last_third_mean"] == 4.5
    assert b["diversity"]["distinct_room_types"] == 2
    assert b["self_consistency"]["requests_with_repeats"] == 0


def test_behavior_self_consistency_and_pairwise_agreement():
    rows = [_decision("a", "room", it=1), _decision("a", "corridor", it=2),
            _decision("b", "room", it=1), _decision("b", "room", it=2)]  # fmt: skip
    b = selection_behavior(rows, [("run", "a"), ("run", "b")])
    assert b["self_consistency"]["same_room_type_across_iterations_rate"] == 0.5
    other = [_decision("a", "room"), _decision("b", "corridor")]
    pair = pairwise_agreement({"x": rows, "y": other})[0]
    assert (pair["requests_compared"], pair["same_room_type_rate"]) == (2, 0.5)


def test_behavior_ignores_failures_and_handles_no_decisions():
    b = selection_behavior([_decision("a", ok=False)], [("run", "a")])
    assert b["decisions"] == 0 and b["diversity"]["room_type_entropy_bits"] is None


# --- report -----------------------------------------------------------------


def test_report_separates_measured_derived_and_subjective_without_a_winner(bundle_copy, tmp_path):
    obs = tmp_path / "obs.json"
    obs.write_text(json.dumps({"observations": [{
        "kind": "subjective", "author": "tester", "date": "2026-09-19",
        "context": "10 minutes of play", "text": "felt snappy"}]}))  # fmt: skip
    text = analyze(bundle_copy, repo_root=REPO_ROOT, observations_path=obs).read_text()
    measured, subjective = text.split("## 6. Subjective observations")
    assert "felt snappy" in subjective and "felt snappy" not in measured
    for heading in ("## 1. Provenance", "## 2. Measured", "## 3. Derived", "## 4. Descriptive",
                    "## 5. Validity"):  # fmt: skip
        assert heading in measured
    # the embedded summarizer block is #15's own text (it says "rank = p * (n - 1)")
    lowered = re.sub(r"```text.*?```", "", text, flags=re.DOTALL).lower()
    for word in ("winner", "fastest", "rank ", "ranking:", "best provider", "recommended"):
        assert word not in lowered.replace("nothing here ranks providers or names a winner", "")


def test_observations_must_be_marked_subjective(tmp_path):
    path = tmp_path / "o.json"
    path.write_text(json.dumps({"observations": [{"kind": "measured", "author": "a", "date": "d",
                                                  "context": "c", "text": "t"}]}))  # fmt: skip
    with pytest.raises(AnalysisError, match="subjective"):
        load_observations(path)
    template = REPO_ROOT / "benchmarks" / "evaluation" / "observations.template.json"
    assert load_observations(template) == []


def test_report_flags_small_samples_and_missing_prices(corpus, tmp_path):
    out = tmp_path / "live"
    params = resolve_parameters(load_protocol(), "smoke", iterations=1, warmup_requests=1)
    run_evaluation(corpus, [resolve_selection("groq", {"GROQ_API_KEY": SECRET})], params, out,
                   repo_root=REPO_ROOT, live=True, vantage_point="home wifi",
                   service_factory=fake_factory, progress=lambda _: None)  # fmt: skip
    text = analyze(out, repo_root=REPO_ROOT).read_text()
    assert "too few for" in text and "p95" in text
    assert "no_price" in text and "home wifi" in text
    assert "billable" not in text or "Live run" in text


def test_summarizer_bridge_reports_absence_instead_of_reimplementing(tmp_path):
    status = run_summarizer(
        tmp_path / "r.json", tmp_path, repo_root=REPO_ROOT, module="no.such.mod"
    )
    assert status["status"] == "unavailable" and "issue #15" in status["reason"]


def test_summarizer_bridge_uses_the_documented_cli(tmp_path, monkeypatch):
    stub = tmp_path / "stub_summarizer.py"
    stub.write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--input', nargs='+')\n"
        "p.add_argument('--format')\n"
        "p.add_argument('--output')\n"
        "a = p.parse_args()\n"
        "open(a.output, 'w').write('stub ' + a.format + ' ' + ','.join(a.input))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    out = tmp_path / "out"
    out.mkdir()
    status = run_summarizer(
        Path("results.json"), out, repo_root=REPO_ROOT, module="stub_summarizer"
    )
    assert status["status"] == "ok"
    assert (out / "summary.csv").read_text() == "stub csv results.json"
    assert set(status["files"]) == {"text", "json", "csv"}


@pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-c", "import benchmarks.summarize"], capture_output=True, cwd=REPO_ROOT
    ).returncode
    != 0,
    reason="benchmarks.summarize (issue #15) is not in this checkout",
)
def test_real_summarizer_accepts_the_bundle_results(bundle_copy):
    analyze(bundle_copy, repo_root=REPO_ROOT)
    summary = json.loads((bundle_copy / "summary.json").read_text())
    assert summary["groups"][0]["provider"] == "rules-baseline"
    assert "p99" in (bundle_copy / "summary.txt").read_text()


# --- repository wiring and documentation -----------------------------------


def _make(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(["make", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          env={**os.environ, **env})  # fmt: skip


def test_live_make_target_refuses_without_explicit_opt_in():
    refused = _make("eval-live")
    assert refused.returncode != 0 and "refusing" in refused.stdout + refused.stderr
    no_select = _make("eval-live", "EVAL_LIVE=1")
    assert no_select.returncode != 0 and "EVAL_SELECT" in no_select.stdout + no_select.stderr


def test_offline_make_targets_need_no_credentials_and_are_not_live():
    makefile = (REPO_ROOT / "Makefile").read_text()
    offline = makefile.split("eval-offline:")[1].split("eval-live:")[0]
    assert "--live" not in offline and "--select rules-baseline" in offline
    assert "sample_replay_results" not in makefile.split("EVAL_CORPUS ")[1].split("godot-lint:")[0]


def test_generated_evidence_and_live_corpora_are_git_ignored():
    ignored = (REPO_ROOT / ".gitignore").read_text()
    for pattern in ("evaluation-output/", "benchmarks/corpus/live/", "pricing.local.json"):
        assert pattern in ignored

    def ignored(path: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT).returncode == 0

    assert ignored("evaluation-output/x/report.md")
    assert ignored("benchmarks/corpus/live/mine/requests.jsonl")
    assert not ignored("benchmarks/corpus/replay-v1/requests.jsonl")


def test_methodology_document_covers_the_required_topics_and_labels_fixtures_synthetic():
    doc = (REPO_ROOT / "docs" / "evaluation-methodology.md").read_text()
    lowered = " ".join(doc.lower().split())  # the document is line-wrapped
    topics = (
        "fixed replay corpus", "live corpus", "warm-up", "sample size", "p50", "p95", "p99",
        "retries", "schema", "normalized cost", "diversity", "repetition", "danger",
        "room type", "network", "provider load", "model", "caching", "cold start", "measured",
        "subjective", "hard-coded winner", "benchmarks.summarize", "reproduc",
    )  # fmt: skip
    for topic in topics:
        assert topic in lowered, topic
    assert "synthetic" in lowered and "sample_replay_results" in doc
    assert "not provider measurements" in lowered


def test_canonical_json_is_stable():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
    assert plan_digest([{"request_id": "b", "success": True, "room": {"x": 1}},
                        {"request_id": "a", "success": False, "room": None,
                         "error_code": "provider_timeout"}]) == plan_digest(
        [{"request_id": "a", "success": False, "room": None, "error_code": "provider_timeout"},
         {"request_id": "b", "success": True, "room": {"x": 1}}])  # fmt: skip
