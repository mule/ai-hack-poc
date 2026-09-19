"""Descriptive behaviour metrics: diversity, repetition, danger progression, agreement.

Room-type and danger *distributions* per provider/model come from the summarizer
(issue #15); this module adds only what a distribution cannot show: how varied,
repetitive and self-consistent the decisions are, and how much providers agree on
the same request. Nothing here ranks or scores providers: these are descriptions,
and whether "more varied" is better is a design call, not a measurement.

Sequence-based figures use the first measured iteration in corpus order, grouped
by ``run_id``, so repeated iterations do not double-count the same decisions.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any


def _signature(room: dict[str, Any]) -> str:
    exits = ",".join(sorted(e["direction"] for e in room.get("exits", [])))
    return f"{room['room_type']}|{room['size']}|d{room['danger']}|{exits}"


def _entropy_bits(counts: Counter[str]) -> float:
    total = sum(counts.values())
    return round(-sum(c / total * math.log2(c / total) for c in counts.values()), 4)


def _slope(values: list[float]) -> float | None:
    """Least-squares slope of values against position (0..1) within one sequence."""
    n = len(values)
    if n < 3:
        return None
    xs = [i / (n - 1) for i in range(n)]
    mean_x, mean_y = sum(xs) / n, sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True)) / denom


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def selection_behavior(
    rows: list[dict[str, Any]], corpus_order: list[tuple[str, str]]
) -> dict[str, Any]:
    """Behaviour of one provider/model. ``corpus_order`` is (run_id, request_id) in file order."""
    ok = [r for r in rows if r.get("success") and r.get("room")]
    first = {r["request_id"]: r["room"] for r in ok if r["iteration"] == 1}

    types = Counter(r["room"]["room_type"] for r in ok)
    signatures = Counter(_signature(r["room"]) for r in ok)

    sequences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run_id, request_id in corpus_order:
        if request_id in first:
            sequences[run_id].append(first[request_id])
    pairs = same = longest = 0
    for rooms in sequences.values():
        streak = 0
        for previous, current in zip([None, *rooms[:-1]], rooms, strict=True):
            if previous is not None:
                pairs += 1
                same += previous["room_type"] == current["room_type"]
            streak = streak + 1 if previous and previous["room_type"] == current["room_type"] else 1
            longest = max(longest, streak)

    third_first: list[float] = []
    third_last: list[float] = []
    slopes: list[float] = []
    for rooms in sequences.values():
        danger = [float(room["danger"]) for room in rooms]
        cut = len(danger) // 3
        if cut:
            third_first += danger[:cut]
            third_last += danger[-cut:]
        slope = _slope(danger)
        if slope is not None:
            slopes.append(slope)

    by_request: dict[str, list[str]] = defaultdict(list)
    for row in ok:
        by_request[row["request_id"]].append(row["room"]["room_type"])
    multi = {rid: kinds for rid, kinds in by_request.items() if len(kinds) > 1}

    return {
        "decisions": len(ok),
        "diversity": {
            "room_type_entropy_bits": _entropy_bits(types) if types else None,
            "distinct_room_types": len(types),
            "distinct_signatures": len(signatures),
            "signature_repeat_rate": _rate(len(ok) - len(signatures), len(ok)),
        },
        "repetition": {
            "basis": "iteration 1, corpus order within each run",
            "consecutive_same_room_type_rate": _rate(same, pairs),
            "longest_same_room_type_streak": longest or None,
        },
        "danger_progression": {
            "basis": "iteration 1, corpus order within each run",
            "first_third_mean": _mean(third_first),
            "last_third_mean": _mean(third_last),
            "mean_slope_per_run": _mean(slopes, digits=4),
        },
        "self_consistency": {
            "requests_with_repeats": len(multi),
            "same_room_type_across_iterations_rate": _rate(
                sum(1 for kinds in multi.values() if len(set(kinds)) == 1), len(multi)
            ),
        },
    }


def _mean(values: list[float], digits: int = 3) -> float | None:
    return round(sum(values) / len(values), digits) if values else None


def pairwise_agreement(
    rows_by_label: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """How often two provider/models decide alike on the same request (iteration 1)."""
    plans = {
        label: {
            r["request_id"]: r["room"]
            for r in rows
            if r["iteration"] == 1 and r.get("success") and r.get("room")
        }
        for label, rows in rows_by_label.items()
    }
    out = []
    for a, b in combinations(sorted(plans), 2):
        shared = sorted(plans[a].keys() & plans[b].keys())
        out.append(
            {
                "a": a,
                "b": b,
                "requests_compared": len(shared),
                "same_room_type_rate": _rate(
                    sum(plans[a][r]["room_type"] == plans[b][r]["room_type"] for r in shared),
                    len(shared),
                ),
                "same_signature_rate": _rate(
                    sum(_signature(plans[a][r]) == _signature(plans[b][r]) for r in shared),
                    len(shared),
                ),
            }
        )
    return out
