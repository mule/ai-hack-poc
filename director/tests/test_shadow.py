"""Shadow evaluation: fanout, isolation, concurrency, failure, lifecycle, bounds, hygiene."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time

import pytest
from fakes import (
    ClassifiedFailureProvider,
    FakeProvider,
    MalformedProvider,
    RaisingProvider,
    SlowProvider,
    make_request,
)
from shadow_fakes import (
    CANARY,
    ExplodingObserver,
    GatedProvider,
    MutatingProvider,
    RecordingObserver,
    StubbornProvider,
    eventually,
    make_shadow_service,
    run_scenario,
)

from dungeon_director import shadow as shadow_module
from dungeon_director.contracts import ErrorKind
from dungeon_director.errors import ProviderError
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget
from dungeon_director.shadow import (
    METRIC_LABEL_NAMES,
    ComparisonMeta,
    ExecutionRecord,
    ExecutionRole,
    ExecutionStatus,
    ShadowEvaluator,
    ShadowStore,
    SkipReason,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def snapshot(service: DirectorService, comparison_id: str | None):
    assert comparison_id is not None
    snap = service.shadow.store.get(comparison_id)
    assert snap is not None
    return snap


def by_provider(snap):
    return {r.provider: r for r in snap.records}


# --- fanout ------------------------------------------------------------------


def test_shadows_receive_the_identical_request_as_independent_copies():
    async def scenario():
        active = GatedProvider("active")
        s1, s2 = GatedProvider("shadow-a"), GatedProvider("shadow-b")
        service = make_shadow_service(active, [s1, s2])
        request = make_request()
        pristine = request.model_dump(mode="json")

        outcome = await service.generate(request)
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return request, pristine, active, s1, s2

    request, pristine, active, s1, s2 = run_scenario(scenario)

    for provider in (active, s1, s2):
        assert len(provider.requests) == 1
        assert provider.requests[0].model_dump(mode="json") == pristine
    assert active.requests[0] is request
    assert s1.requests[0] is not request and s2.requests[0] is not request
    assert s1.requests[0] is not s2.requests[0], "each shadow gets its own copy"
    assert request.model_dump(mode="json") == pristine


def test_shadow_targets_resolve_their_own_models():
    async def scenario():
        active = GatedProvider("active", models=("a1", "a2"))
        multi = GatedProvider("multi", models=("m1", "m2"))
        service = make_shadow_service(
            active,
            [multi],
            targets=[ShadowTarget("multi"), ShadowTarget("multi", "m2")],
        )
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return multi

    multi = run_scenario(scenario)

    assert sorted(multi.models_seen) == ["m1", "m2"], "bare target = the provider's own default"


def test_a_shadow_that_mutates_its_request_cannot_change_what_anyone_else_sees():
    async def scenario():
        active = GatedProvider("active")
        vandal = MutatingProvider("vandal")
        honest = GatedProvider("honest")
        service = make_shadow_service(active, [vandal, honest])
        request = make_request()
        pristine = request.model_dump(mode="json")

        outcome = await service.generate(request)
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return pristine, request, outcome, active, honest

    pristine, request, outcome, active, honest = run_scenario(scenario)

    assert request.model_dump(mode="json") == pristine
    assert active.requests[0].model_dump(mode="json") == pristine
    assert honest.requests[0].model_dump(mode="json") == pristine
    assert outcome.response.success is True


def test_same_target_may_shadow_the_active_provider():
    async def scenario():
        active = GatedProvider("twin")
        service = make_shadow_service(active, targets=[ShadowTarget("twin")])
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return active, snapshot(service, outcome.comparison_id)

    active, snap = run_scenario(scenario)

    assert active.calls == 2
    assert {r.role for r in snap.records} == {ExecutionRole.ACTIVE, ExecutionRole.SHADOW}


def test_an_invalid_active_selection_launches_no_shadows():
    async def scenario():
        active, shadow = GatedProvider("active"), GatedProvider("shadow")
        service = make_shadow_service(active, [shadow])
        outcome = await service.generate(make_request(), provider="no-such-provider")
        await asyncio.sleep(0.02)
        return service, active, shadow, outcome

    service, active, shadow, outcome = run_scenario(scenario)

    assert outcome.status_code == 404
    assert outcome.comparison_id is None
    assert shadow.calls == 0 and active.calls == 0
    assert len(service.shadow.store) == 0


def test_shadow_mode_off_changes_nothing():
    async def scenario():
        registry = ProviderRegistry()
        provider = FakeProvider("solo")
        registry.register(provider)
        service = DirectorService(registry, DirectorSettings(default_provider="solo"))
        before = len(asyncio.all_tasks())
        outcome = await service.generate(make_request())
        return service, outcome, before, len(asyncio.all_tasks())

    service, outcome, before, after = run_scenario(scenario)

    assert service.shadow is None
    assert outcome.comparison_id is None and outcome.response.success is True
    assert before == after


# --- the active response is untouched -----------------------------------------


def test_active_outcome_is_identical_with_and_without_hostile_shadows():
    async def scenario():
        baseline = make_shadow_service(FakeProvider("active"), [])
        baseline_outcome = await baseline.generate(make_request())

        active = FakeProvider("active")
        shadows = [
            RaisingProvider(RuntimeError(CANARY), "boom"),
            ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "slow down", "limited"),
            MalformedProvider({"nonsense": True}, "garbage"),
            SlowProvider("slow"),
            MutatingProvider("vandal"),
        ]
        service = make_shadow_service(active, shadows, shadow_timeout=0.05)
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete, timeout=3)
        return baseline_outcome, outcome

    baseline, outcome = run_scenario(scenario)

    def stable(o):
        body = o.response.model_dump(mode="json")
        for volatile in ("started_at", "completed_at", "latency_ms"):
            body["metadata"].pop(volatile)
        return body, o.status_code

    assert stable(outcome) == stable(baseline)
    assert outcome.response.room.room_id == "fake-room-1"
    assert "cmp-" not in outcome.response.model_dump_json(), "the body never carries the id"
    assert outcome.comparison_id and baseline.comparison_id is None


def test_only_the_active_room_is_returned_even_when_shadows_answer_first():
    async def scenario():
        gate = asyncio.Event()
        active = GatedProvider("active", gate=gate)
        fast = GatedProvider("fast")
        service = make_shadow_service(active, [fast])
        task = asyncio.create_task(service.generate(make_request()))
        await eventually(lambda: fast.calls == 1)
        await eventually(lambda: len(service.shadow.store.recent(1)[0].records) == 1)
        assert not task.done()
        gate.set()
        return await task

    outcome = run_scenario(scenario)

    assert outcome.response.room.room_id == "active-room"
    assert outcome.response.metadata.provider == "active"


def test_active_failure_is_recorded_and_shadows_still_complete():
    async def scenario():
        active = SlowProvider("active")
        shadow = GatedProvider("shadow")
        service = make_shadow_service(active, [shadow], timeout=0.05)
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return outcome, snapshot(service, outcome.comparison_id)

    outcome, snap = run_scenario(scenario)

    assert outcome.status_code == 504 and outcome.response.success is False
    assert snap.active.status is ExecutionStatus.FAILURE
    assert snap.active.outcome.response.metadata.error.code is ErrorKind.PROVIDER_TIMEOUT
    assert snap.shadows[0].status is ExecutionStatus.SUCCESS


# --- concurrency ------------------------------------------------------------


def test_active_and_shadows_run_concurrently_not_in_sequence():
    async def scenario():
        gate = asyncio.Event()
        providers = [GatedProvider(name, gate=gate) for name in ("active", "s-one", "s-two")]
        service = make_shadow_service(providers[0], providers[1:], drain_seconds=1)
        task = asyncio.create_task(service.generate(make_request()))

        # If fanout were sequential, the gated active call would starve the
        # shadows and this would time out. All three must be inside provider
        # code at the same time, before any is released.
        await asyncio.wait_for(asyncio.gather(*(p.started.wait() for p in providers)), 2)
        assert not task.done()
        assert service.shadow.pending == 2
        gate.set()
        outcome = await task
        await eventually(lambda: service.shadow.pending == 0)
        return outcome, service

    outcome, service = run_scenario(scenario)

    assert outcome.response.success is True
    assert snapshot(service, outcome.comparison_id).complete


def test_a_slow_shadow_never_delays_the_active_answer():
    async def scenario():
        never = asyncio.Event()
        slow = GatedProvider("slow", gate=never)
        service = make_shadow_service(FakeProvider("active"), [slow], drain_seconds=0.05)

        started = time.perf_counter()
        outcome = await service.generate(make_request())
        elapsed = time.perf_counter() - started

        await asyncio.wait_for(slow.started.wait(), 2)
        pending_at_return = service.shadow.pending
        snap = snapshot(service, outcome.comparison_id)
        active_recorded, complete = snap.active is not None, snap.complete
        never.set()
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return elapsed, outcome, pending_at_return, active_recorded, complete

    elapsed, outcome, pending, active_recorded, complete = run_scenario(scenario)

    assert outcome.response.success is True
    assert elapsed < 0.25, f"the active answer waited on a shadow ({elapsed:.3f}s)"
    assert pending == 1, "the shadow was still running when the active answer returned"
    assert active_recorded and not complete


def test_many_concurrent_requests_keep_their_comparisons_apart():
    async def scenario():
        active = FakeProvider("active")
        shadows = [GatedProvider("s-one"), GatedProvider("s-two")]
        service = make_shadow_service(active, shadows)
        requests = [make_request(request_id=f"req-{i}") for i in range(20)]
        outcomes = await asyncio.gather(*(service.generate(r) for r in requests))
        await eventually(lambda: all(snapshot(service, o.comparison_id).complete for o in outcomes))
        return requests, outcomes, service

    requests, outcomes, service = run_scenario(scenario)

    assert len({o.comparison_id for o in outcomes}) == 20
    for request, outcome in zip(requests, outcomes, strict=True):
        snap = snapshot(service, outcome.comparison_id)
        assert snap.meta.request_id == request.request_id
        assert len(snap.records) == 3
        assert {r.request_id for r in snap.records} == {request.request_id}
        assert {r.request_id for r in (snap.active, *snap.shadows)} == {request.request_id}
        assert snap.active.outcome.response.request_id == request.request_id


# --- shadow failures ---------------------------------------------------------


def test_failing_shadows_are_recorded_as_canonical_failures_and_never_escape():
    async def scenario():
        shadows = [
            RaisingProvider(RuntimeError(CANARY), "boom"),
            ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "slow down", "limited"),
            MalformedProvider({"nonsense": True}, "garbage"),
        ]
        service = make_shadow_service(FakeProvider("active"), shadows)
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return outcome, by_provider(snapshot(service, outcome.comparison_id))

    outcome, records = run_scenario(scenario)

    assert outcome.response.success is True
    assert records["boom"].outcome.response.metadata.error.code is ErrorKind.PROVIDER_ERROR
    assert records["limited"].outcome.status_code == 429
    assert records["limited"].outcome.response.metadata.error.code is ErrorKind.RATE_LIMITED
    assert records["garbage"].outcome.response.metadata.error.code is ErrorKind.SCHEMA_VIOLATION
    for name in ("boom", "limited", "garbage"):
        assert records[name].status is ExecutionStatus.FAILURE
        assert records[name].role is ExecutionRole.SHADOW


def test_shadow_uses_its_own_timeout_and_does_not_affect_the_active_deadline():
    async def scenario():
        service = make_shadow_service(
            FakeProvider("active"), [SlowProvider("slow")], timeout=5, shadow_timeout=0.05
        )
        started = time.perf_counter()
        outcome = await service.generate(make_request())
        active_elapsed = time.perf_counter() - started
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return active_elapsed, outcome, snapshot(service, outcome.comparison_id)

    active_elapsed, outcome, snap = run_scenario(scenario)

    assert outcome.response.success is True and active_elapsed < 0.25
    shadow = snap.shadows[0]
    assert shadow.status is ExecutionStatus.FAILURE
    assert shadow.outcome.response.metadata.error.code is ErrorKind.PROVIDER_TIMEOUT
    assert shadow.outcome.status_code == 504
    assert 40 <= shadow.duration_ms < 1000


def test_a_shadow_that_cancels_itself_or_is_cancelled_is_recorded_and_harmless():
    async def scenario():
        never = asyncio.Event()
        victim = GatedProvider("victim", gate=never)
        service = make_shadow_service(FakeProvider("active"), [victim])
        outcome = await service.generate(make_request())
        await asyncio.wait_for(victim.started.wait(), 2)

        (task,) = service.shadow._tasks
        task.cancel()
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return service, outcome, victim

    service, outcome, victim = run_scenario(scenario)

    assert outcome.response.success is True
    assert victim.cancelled is True
    shadow = snapshot(service, outcome.comparison_id).shadows[0]
    assert shadow.status is ExecutionStatus.CANCELLED and shadow.outcome is None
    assert service.shadow.pending == 0


def test_a_shadow_cancelled_before_its_first_step_still_gets_a_record():
    async def scenario():
        shadow = GatedProvider("shadow")
        service = make_shadow_service(FakeProvider("active"), [shadow])
        comparison = service.shadow.begin(make_request(), provider="active", model="fake-model")
        comparison.abort(cancelled=True)  # no await in between: the task never ran
        await eventually(lambda: service.shadow.store.get(comparison.comparison_id).complete)
        return shadow, service.shadow.store.get(comparison.comparison_id)

    shadow, snap = run_scenario(scenario)

    assert shadow.calls == 0
    assert snap.active.status is ExecutionStatus.CANCELLED
    assert snap.shadows[0].status is ExecutionStatus.CANCELLED


def test_a_crash_around_a_shadow_call_is_consumed_and_recorded_as_error(caplog):
    caplog.set_level(logging.DEBUG)

    async def crashing_runner(request, provider, model, timeout):
        raise RuntimeError(CANARY)

    async def scenario():
        registry = ProviderRegistry()
        registry.register(FakeProvider("shadow"))
        evaluator = ShadowEvaluator(
            ShadowSettings(targets=(ShadowTarget("shadow"),)),
            registry,
            crashing_runner,
            default_timeout=1,
        )
        comparison = evaluator.begin(make_request(), provider="active", model="m")
        await eventually(lambda: evaluator.pending == 0)
        return evaluator.store.get(comparison.comparison_id)

    snap = run_scenario(scenario)

    assert snap.shadows[0].status is ExecutionStatus.ERROR
    assert snap.shadows[0].outcome is None
    assert "RuntimeError" in caplog.text and CANARY not in caplog.text


def test_unavailable_or_unknown_shadow_targets_degrade_to_skipped_records(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        down = FakeProvider("down", available=False)
        service = make_shadow_service(
            FakeProvider("active"),
            [],
            targets=[
                ShadowTarget("down"),
                ShadowTarget("ghost"),
                ShadowTarget("active", "no-such-model"),
                # Valid-looking ids that are really a pasted credential.
                ShadowTarget(CANARY),
                ShadowTarget("active", CANARY),
            ],
            extra=[down],
        )
        outcomes = [await service.generate(make_request(request_id=f"r-{i}")) for i in range(3)]
        return service, outcomes

    service, outcomes = run_scenario(scenario)

    for outcome in outcomes:
        assert outcome.response.success is True
        snap = snapshot(service, outcome.comparison_id)
        assert snap.complete
        assert {r.skip_reason for r in snap.shadows} == {
            SkipReason.PROVIDER_UNAVAILABLE,
            SkipReason.UNKNOWN_PROVIDER,
            SkipReason.UNKNOWN_MODEL,
        }
        assert all(r.status is ExecutionStatus.SKIPPED and r.outcome is None for r in snap.shadows)
        assert all(
            r.metric_labels()["provider"] in {"down", "other", "active"}
            and CANARY not in r.metric_labels().values()
            for r in snap.shadows
        )
    warnings = [r for r in caplog.records if "cannot run" in r.getMessage()]
    assert len(warnings) == 5, "one startup warning per bad target, not one per request"
    assert CANARY not in caplog.text, "unregistered ids and models are never printed"
    assert [(t.provider, t.model) for t in service.shadow.registered_targets] == [("down", None)]


def test_shadow_launch_failure_never_fails_the_active_call(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        service = make_shadow_service(FakeProvider("active"), [GatedProvider("shadow")])

        def broken_begin(*args, **kwargs):
            raise RuntimeError(CANARY)

        monkeypatch.setattr(service.shadow, "begin", broken_begin)
        return await service.generate(make_request())

    outcome = run_scenario(scenario)

    assert outcome.response.success is True and outcome.comparison_id is None
    assert "RuntimeError" in caplog.text and CANARY not in caplog.text


def test_one_target_failing_to_launch_does_not_stop_the_others(monkeypatch):
    async def scenario():
        good = GatedProvider("good")
        service = make_shadow_service(
            FakeProvider("active"),
            [],
            targets=[ShadowTarget("bad"), ShadowTarget("good")],
            extra=[GatedProvider("bad"), good],
        )
        real = service.shadow._selection_problem

        def flaky(target):
            if target.provider == "bad":
                raise RuntimeError(CANARY)
            return real(target)

        monkeypatch.setattr(service.shadow, "_selection_problem", flaky)
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return by_provider(snapshot(service, outcome.comparison_id))

    records = run_scenario(scenario)

    assert records["bad"].skip_reason is SkipReason.LAUNCH_FAILED
    assert records["good"].status is ExecutionStatus.SUCCESS


# --- client cancellation ------------------------------------------------------


def test_cancelling_the_active_request_propagates_and_cancels_its_shadows():
    async def scenario():
        never = asyncio.Event()
        active = GatedProvider("active", gate=never)
        shadow = GatedProvider("shadow", gate=never)
        service = make_shadow_service(active, [shadow])
        task = asyncio.create_task(service.generate(make_request()))
        await asyncio.wait_for(asyncio.gather(active.started.wait(), shadow.started.wait()), 2)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled(), "cancellation must not be swallowed or converted"

        comparison_id = service.shadow.store.recent(1)[0].meta.comparison_id
        await eventually(lambda: snapshot(service, comparison_id).complete)
        await eventually(lambda: service.shadow.pending == 0)
        return active, shadow, snapshot(service, comparison_id)

    active, shadow, snap = run_scenario(scenario)

    assert active.cancelled and shadow.cancelled
    assert snap.active.status is ExecutionStatus.CANCELLED
    assert snap.shadows[0].status is ExecutionStatus.CANCELLED


def test_cancellation_is_not_delayed_by_a_shadow_that_ignores_it():
    async def scenario():
        never = asyncio.Event()
        stubborn = StubbornProvider("stubborn")
        active = GatedProvider("active", gate=never)
        service = make_shadow_service(active, [stubborn])
        task = asyncio.create_task(service.generate(make_request()))
        await asyncio.wait_for(asyncio.gather(active.started.wait(), stubborn.started.wait()), 2)

        started = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - started
        stubborn.release.set()  # let the stubborn shadow finish so the loop can close
        await eventually(lambda: service.shadow.pending == 0)
        return elapsed

    assert run_scenario(scenario) < 0.25


# --- lifecycle ---------------------------------------------------------------


def test_aclose_drains_shadows_that_finish_within_the_grace_period():
    async def scenario():
        gate = asyncio.Event()
        shadow = GatedProvider("shadow", gate=gate)
        service = make_shadow_service(FakeProvider("active"), [shadow], drain_seconds=2)
        outcome = await service.generate(make_request())
        await asyncio.wait_for(shadow.started.wait(), 2)
        asyncio.get_running_loop().call_later(0.05, gate.set)

        await service.aclose()
        return service, shadow, snapshot(service, outcome.comparison_id)

    service, shadow, snap = run_scenario(scenario)

    assert service.shadow.pending == 0 and not shadow.cancelled
    assert snap.shadows[0].status is ExecutionStatus.SUCCESS


def test_aclose_cancels_shadows_that_outlive_the_grace_period_and_is_idempotent():
    async def scenario():
        never = asyncio.Event()
        slow = GatedProvider("slow", gate=never)
        service = make_shadow_service(FakeProvider("active"), [slow], drain_seconds=0.05)
        outcome = await service.generate(make_request())
        await asyncio.wait_for(slow.started.wait(), 2)

        started = time.perf_counter()
        await service.aclose()
        elapsed = time.perf_counter() - started
        await service.aclose()

        after = await service.generate(make_request(request_id="after-close"))
        await asyncio.sleep(0.02)
        return service, slow, elapsed, outcome, after

    service, slow, elapsed, outcome, after = run_scenario(scenario)

    assert 0.04 <= elapsed < 1.0
    assert slow.cancelled and service.shadow.pending == 0
    assert snapshot(service, outcome.comparison_id).shadows[0].status is ExecutionStatus.CANCELLED
    assert after.response.success is True and after.comparison_id is None
    assert slow.calls == 1, "no shadow work starts after shutdown"


def test_aclose_is_bounded_even_if_a_shadow_ignores_cancellation(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(shadow_module, "_CANCEL_GRACE_SECONDS", 0.05)

    async def scenario():
        stubborn = StubbornProvider("stubborn")
        service = make_shadow_service(FakeProvider("active"), [stubborn], drain_seconds=0.05)
        await service.generate(make_request())
        await asyncio.wait_for(stubborn.started.wait(), 2)

        started = time.perf_counter()
        await service.aclose()
        elapsed = time.perf_counter() - started
        assert service.shadow.pending == 1, "abandoned, not silently forgotten"
        stubborn.release.set()
        await eventually(lambda: service.shadow.pending == 0)
        return elapsed, stubborn

    elapsed, stubborn = run_scenario(scenario)

    assert elapsed < 1.0 and stubborn.swallowed == 1
    assert "ignored cancellation at shutdown" in caplog.text


def test_cancelling_aclose_cancels_the_shadows_before_propagating():
    async def scenario():
        slow = GatedProvider("slow", gate=asyncio.Event())
        service = make_shadow_service(FakeProvider("active"), [slow], drain_seconds=30)
        await service.generate(make_request())
        await asyncio.wait_for(slow.started.wait(), 2)

        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0.02)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await eventually(lambda: service.shadow.pending == 0)
        return slow

    assert run_scenario(scenario).cancelled


def test_launching_after_close_is_skipped_as_shutting_down():
    async def scenario():
        shadow = GatedProvider("shadow")
        service = make_shadow_service(FakeProvider("active"), [shadow])
        await service.aclose()
        comparison = service.shadow.begin(make_request(), provider="active", model="fake-model")
        return shadow, service.shadow.store.get(comparison.comparison_id)

    shadow, snap = run_scenario(scenario)

    assert shadow.calls == 0
    assert snap.shadows[0].skip_reason is SkipReason.SHUTTING_DOWN


def test_no_shadow_tasks_remain_after_a_full_lifecycle():
    async def scenario():
        shadows = [GatedProvider("s-one"), GatedProvider("s-two", gate=asyncio.Event())]
        service = make_shadow_service(FakeProvider("active"), shadows, drain_seconds=0.05)
        for i in range(5):
            await service.generate(make_request(request_id=f"r-{i}"))
        await service.aclose()
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert run_scenario(scenario) == []


# --- bounds ------------------------------------------------------------------


def test_in_flight_shadow_calls_are_capped_and_overflow_is_skipped_not_queued():
    async def scenario():
        gate = asyncio.Event()
        shadows = [GatedProvider(f"s-{i}", gate=gate) for i in range(3)]
        service = make_shadow_service(FakeProvider("active"), shadows, max_in_flight=2)

        first = await service.generate(make_request(request_id="first"))
        second = await service.generate(make_request(request_id="second"))
        peak = service.shadow.pending
        first_snap = snapshot(service, first.comparison_id)
        second_snap = snapshot(service, second.comparison_id)
        gate.set()
        await eventually(lambda: service.shadow.pending == 0)
        return peak, first_snap, second_snap, shadows

    peak, first_snap, second_snap, shadows = run_scenario(scenario)

    assert peak == 2
    assert [r.skip_reason for r in first_snap.shadows] == [SkipReason.OVERLOADED]
    assert {r.skip_reason for r in second_snap.shadows} == {SkipReason.OVERLOADED}
    assert sum(p.calls for p in shadows) == 2, "skipped targets were never called"


def test_the_store_keeps_only_the_newest_comparisons():
    async def scenario():
        service = make_shadow_service(FakeProvider("active"), [FakeProvider("s")], store_size=3)
        outcomes = []
        for i in range(10):
            outcomes.append(await service.generate(make_request(request_id=f"r-{i}")))
            await eventually(lambda: service.shadow.pending == 0)
        return service, outcomes

    service, outcomes = run_scenario(scenario)
    store = service.shadow.store

    assert len(store) == 3 and store.evicted == 7
    assert [s.meta.comparison_id for s in store.recent(10)] == [
        o.comparison_id for o in reversed(outcomes[-3:])
    ]
    assert store.get(outcomes[0].comparison_id) is None
    assert store.recent(0) == []


def test_late_records_for_an_evicted_comparison_are_dropped_and_counted():
    async def scenario():
        gate = asyncio.Event()
        shadow = GatedProvider("shadow", gate=gate)
        service = make_shadow_service(FakeProvider("active"), [shadow], store_size=1)
        old = await service.generate(make_request(request_id="old"))
        new = await service.generate(make_request(request_id="new"))
        gate.set()
        await eventually(lambda: service.shadow.pending == 0)
        return service, old, new

    service, old, new = run_scenario(scenario)
    store = service.shadow.store

    assert store.get(old.comparison_id) is None, "evicted comparisons are not resurrected"
    assert store.dropped_records == 1
    assert store.get(new.comparison_id).complete


def test_store_never_holds_more_records_than_a_comparison_expects():
    store = ShadowStore(4)
    meta = ComparisonMeta("cmp-x", "req", "run", _now(), expected=2)
    store.comparison_started(meta)

    for status in ExecutionStatus:
        store.execution_finished(_record("cmp-x", status=status))

    assert len(store.get("cmp-x").records) == 2
    assert store.dropped_records == len(ExecutionStatus) - 2
    with pytest.raises(ValueError):
        ShadowStore(0)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _record(comparison_id="cmp-x", *, status=ExecutionStatus.SUCCESS, **overrides):
    fields = {
        "comparison_id": comparison_id,
        "role": ExecutionRole.SHADOW,
        "provider": "p",
        "model": "m",
        "registered": True,
        "status": status,
        "request_id": "req",
        "run_id": "run",
        "started_at": _now(),
        "completed_at": _now(),
        "duration_ms": 1.0,
    }
    fields.update(overrides)
    return ExecutionRecord(**fields)


def test_comparison_ids_are_bounded_unique_and_valid_identifiers():
    async def scenario():
        service = make_shadow_service(FakeProvider("active"), [FakeProvider("s")], store_size=4)
        ids = []
        for _ in range(300):
            outcome = await service.generate(make_request())  # the very same request_id every time
            ids.append(outcome.comparison_id)
        return ids

    ids = run_scenario(scenario)

    assert len(set(ids)) == 300, "a repeated request id must not reuse a comparison id"
    assert all(_ID_RE.match(i) and len(i) <= 64 for i in ids)
    assert all("req" not in i for i in ids), "ids must not be derived from request ids"


# --- records and metric labels --------------------------------------------------


def test_records_preserve_the_raw_canonical_outcome_with_timing_usage_and_metadata():
    async def scenario():
        service = make_shadow_service(
            FakeProvider("active"),
            [
                GatedProvider("rich"),
                ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "no", "limited"),
            ],
        )
        request = make_request()
        outcome = await service.generate(request)
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return request, outcome, snapshot(service, outcome.comparison_id)

    request, outcome, snap = run_scenario(scenario)
    rich, limited = by_provider(snap)["rich"], by_provider(snap)["limited"]

    assert rich.outcome.status_code == 200
    metadata = rich.outcome.response.metadata
    assert (metadata.provider, metadata.model) == ("rich", "fake-model")
    assert metadata.usage.input_tokens == 11 and metadata.usage.output_tokens == 7
    assert metadata.provider_metadata == {"served_by": "rich"}
    assert metadata.latency_ms is not None and metadata.started_at <= metadata.completed_at
    assert rich.outcome.response.room.room_id == "rich-room"
    assert (rich.request_id, rich.run_id) == (request.request_id, request.run_id)
    assert rich.outcome.response.request_id == request.request_id
    assert rich.outcome.comparison_id == snap.meta.comparison_id
    assert rich.started_at <= rich.completed_at and rich.duration_ms >= 0

    assert limited.outcome.status_code == 429
    assert limited.outcome.response.metadata.error.code is ErrorKind.RATE_LIMITED

    assert snap.active.outcome.response == outcome.response
    assert snap.active.role is ExecutionRole.ACTIVE
    assert snap.meta.expected == 3 and snap.complete


def test_the_stored_active_outcome_is_a_private_copy_and_records_are_immutable():
    async def scenario():
        service = make_shadow_service(FakeProvider("active"), [FakeProvider("s")])
        outcome = await service.generate(make_request())
        outcome.response.metadata.provider = "tampered"
        outcome.response.room.danger = 5
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return snapshot(service, outcome.comparison_id)

    snap = run_scenario(scenario)

    stored = snap.active.outcome.response
    assert stored.metadata.provider == "active" and stored.room.danger == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.active.status = ExecutionStatus.FAILURE
    assert isinstance(snap.records, tuple)


def test_metric_labels_are_bounded_and_never_contain_unbounded_ids():
    async def scenario():
        service = make_shadow_service(
            FakeProvider("active"),
            [ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, "no", "limited")],
            targets=[ShadowTarget("limited"), ShadowTarget("typo-provider-xyz")],
        )
        snaps = []
        for i in range(40):
            outcome = await service.generate(make_request(request_id=f"req-{i}", run_id=f"run-{i}"))
            cid = outcome.comparison_id
            await eventually(lambda cid=cid: snapshot(service, cid).complete)
            snaps.append(snapshot(service, cid))
        return snaps

    snaps = run_scenario(scenario)

    label_sets = set()
    for snap in snaps:
        for record in snap.records:
            labels = record.metric_labels()
            assert set(labels) == set(METRIC_LABEL_NAMES)
            for unbounded in (snap.meta.comparison_id, record.request_id, record.run_id):
                assert all(unbounded not in value for value in labels.values())
            label_sets.add(tuple(sorted(labels.items())))

    assert len(label_sets) == 3, "40 distinct requests must not create new label combinations"
    rendered = {dict(s)["provider"] for s in label_sets}
    assert "typo-provider-xyz" not in rendered and "other" in rendered
    reasons = {dict(s)["reason"] for s in label_sets}
    assert reasons == {"none", "rate_limited", "unknown_provider"}


def test_record_labels_hide_unregistered_active_identifiers():
    record = _record(provider="whatever-the-client-typed", model="x", registered=False)

    labels = record.metric_labels()

    assert labels["provider"] == "other" and labels["model"] == "other"


# --- observers ---------------------------------------------------------------


def test_observers_see_every_execution_and_a_broken_observer_harms_nothing(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        recorder, exploding = RecordingObserver(), ExplodingObserver(f"leak {CANARY}")
        service = make_shadow_service(
            FakeProvider("active"),
            [GatedProvider("s-one"), RaisingProvider(RuntimeError("x"), "boom")],
            observers=[exploding, recorder],
        )
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return outcome, recorder, exploding, snapshot(service, outcome.comparison_id)

    outcome, recorder, exploding, snap = run_scenario(scenario)

    assert outcome.response.success is True
    assert len(recorder.started) == 1 and len(recorder.finished) == 3
    assert {r.comparison_id for r in recorder.finished} == {outcome.comparison_id}
    assert exploding.calls == 4, "the broken observer was still called for every event"
    assert snap.complete, "the store (an observer too) is unaffected by the broken one"
    assert "RuntimeError" in caplog.text and CANARY not in caplog.text


# --- secret hygiene -----------------------------------------------------------


def test_secrets_in_provider_failures_reach_neither_logs_nor_records(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        leaky = RaisingProvider(
            ProviderError(ErrorKind.PROVIDER_ERROR, f"bad key {CANARY}", raw_excerpt=CANARY),
            "leaky",
        )
        service = make_shadow_service(
            FakeProvider("active"),
            [leaky, RaisingProvider(RuntimeError(f"token={CANARY}"), "crashy")],
        )
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return outcome, snapshot(service, outcome.comparison_id)

    outcome, snap = run_scenario(scenario)

    everything = caplog.text + repr(snap) + outcome.response.model_dump_json()
    for record in snap.records:
        if record.outcome is not None:
            everything += record.outcome.response.model_dump_json()
    assert CANARY not in everything
    assert "shadow provider leaky/" in caplog.text, "the logs still say who failed"


def test_shadow_log_lines_are_tagged_and_active_lines_are_not(caplog):
    caplog.set_level(logging.DEBUG)

    async def scenario():
        service = make_shadow_service(
            RaisingProvider(RuntimeError("x"), "act"), [RaisingProvider(RuntimeError("y"), "shd")]
        )
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)

    run_scenario(scenario)

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("shadow provider shd/") for m in messages)
    assert any(m.startswith("provider act/") for m in messages)


def test_a_provider_result_object_is_not_shared_between_active_and_shadow():
    """Two targets returning one shared ProviderResult must not couple their records."""

    class SharedResultProvider(FakeProvider):
        shared = None

        async def decide(self, request):
            if SharedResultProvider.shared is None:
                SharedResultProvider.shared = await super().decide(request)
            return ProviderResult(
                payload=SharedResultProvider.shared.payload,
                provider_metadata=dict(SharedResultProvider.shared.provider_metadata),
            )

    async def scenario():
        service = make_shadow_service(
            SharedResultProvider("active"), [SharedResultProvider("twin")]
        )
        outcome = await service.generate(make_request())
        await eventually(lambda: snapshot(service, outcome.comparison_id).complete)
        return outcome, snapshot(service, outcome.comparison_id)

    outcome, snap = run_scenario(scenario)
    outcome.response.room.exits.clear()

    twin = by_provider(snap)["twin"]
    assert twin.outcome.response.room.exits, "mutating the active response leaves shadows alone"
