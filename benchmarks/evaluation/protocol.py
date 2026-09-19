"""The evaluation protocol definition (``protocol.json``) and its arithmetic."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

PROTOCOL_PATH = Path(__file__).with_name("protocol.json")


class ProtocolError(ValueError):
    """The protocol file or the requested overrides are invalid."""


def load_protocol(path: str | Path = PROTOCOL_PATH) -> dict[str, Any]:
    try:
        protocol = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProtocolError(f"cannot read protocol {path}: {exc}") from exc
    for key in ("protocol_id", "protocol_version", "tiers", "percentiles", "min_tail_samples"):
        if key not in protocol:
            raise ProtocolError(f"{path}: missing {key!r}")
    return protocol


def resolve_parameters(
    protocol: dict[str, Any],
    tier: str,
    *,
    warmup_requests: int | None = None,
    iterations: int | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Effective run parameters: the tier's values plus explicitly recorded overrides."""
    tiers = protocol["tiers"]
    if tier not in tiers:
        raise ProtocolError(f"unknown tier {tier!r}; choose one of {', '.join(tiers)}")
    params = {
        "tier": tier,
        "warmup_requests": tiers[tier]["warmup_requests"],
        "iterations": tiers[tier]["iterations"],
        "timeout_seconds": protocol["default_timeout_seconds"],
        "concurrency": protocol["concurrency"],
        "order_seed": protocol["order_seed"],
    }
    overrides: dict[str, Any] = {}
    for name, value in (
        ("warmup_requests", warmup_requests),
        ("iterations", iterations),
        ("timeout_seconds", timeout_seconds),
    ):
        if value is not None:
            params[name] = value
            overrides[name] = value
    if params["warmup_requests"] < 0 or params["iterations"] < 1:
        raise ProtocolError("warmup_requests must be >= 0 and iterations >= 1")
    if not 0 < params["timeout_seconds"] <= 300:
        raise ProtocolError("timeout_seconds must be greater than 0 and at most 300")
    params["overrides"] = overrides
    return params


def min_samples_for(p: float, min_tail_samples: int) -> int:
    """Smallest sample count for which percentile ``p`` has ``min_tail_samples`` above it."""
    # round first: 10 / (1 - 0.9) is 100.00000000000003 in floating point
    return math.ceil(round(min_tail_samples / (1.0 - p), 9))


def sample_adequacy(n: int, protocol: dict[str, Any]) -> dict[str, Any]:
    """Which percentiles ``n`` measured samples can support."""
    tail = protocol["min_tail_samples"]
    result = {}
    for name, p in protocol["percentiles"].items():
        needed = min_samples_for(p, tail)
        result[name] = {"min_samples": needed, "reliable": n >= needed}
    return {"samples": n, "percentiles": result}
