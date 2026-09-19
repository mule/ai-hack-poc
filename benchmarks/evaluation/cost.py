"""Normalized cost: measured token counts times a *cited* price table.

Adapters do not price their calls, so ``usage.estimated_cost_usd`` is usually
absent for hosted providers (the summarizer of issue #15 reports that value only
where a provider gave it). This module derives a comparable figure from the
tokens that were measured and prices the operator supplies. Prices change and
differ per plan, so none are built in: an entry with any price must cite a
``source`` and a ``retrieved_on`` date, and a missing price yields an explicit
"no price" result, never zero.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

PRICING_TEMPLATE = Path(__file__).with_name("pricing.template.json")
_PRICE_FIELDS = ("input_usd_per_1m_tokens", "output_usd_per_1m_tokens", "per_request_usd")


class PricingError(ValueError):
    """The price table is malformed or has an unsourced price."""


def load_pricing(path: str | Path = PRICING_TEMPLATE) -> dict[tuple[str, str], dict[str, Any]]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PricingError(f"cannot read price table {path}: {exc}") from exc
    entries = document.get("prices") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise PricingError(f"{path}: expected an object with a 'prices' list")
    table: dict[tuple[str, str], dict[str, Any]] = {}
    for index, entry in enumerate(entries, start=1):
        where = f"{path}: prices[{index}]"
        if not isinstance(entry, dict) or not entry.get("provider") or not entry.get("model"):
            raise PricingError(f"{where}: 'provider' and 'model' are required")
        priced = False
        for name in _PRICE_FIELDS:
            value = entry.get(name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise PricingError(f"{where}: {name} must be a non-negative number or null")
            priced = True
        if priced and not (entry.get("source") and entry.get("retrieved_on")):
            raise PricingError(f"{where}: a price needs 'source' and 'retrieved_on'")
        key = (entry["provider"], entry["model"])
        if key in table:
            raise PricingError(f"{where}: duplicate entry for {key[0]}/{key[1]}")
        table[key] = entry
    return table


def derive_cost(rows: list[dict[str, Any]], entry: dict[str, Any] | None) -> dict[str, Any]:
    """Cost of ``rows`` (every measured request, failures included) under ``entry``.

    Failed calls are charged because a provider that answered badly still spent
    tokens. Where token usage is missing the figure covers only the requests that
    reported it and says so.
    """
    total = len(rows)
    if entry is None or all(entry.get(name) is None for name in _PRICE_FIELDS):
        return {"status": "no_price", "requests": total}
    per_request = entry.get("per_request_usd") or 0.0
    in_price = entry.get("input_usd_per_1m_tokens")
    out_price = entry.get("output_usd_per_1m_tokens")
    needs_tokens = in_price is not None or out_price is not None

    costed = 0
    costed_ok = 0
    spend = 0.0
    for row in rows:
        usage = row.get("usage") or {}
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
        if needs_tokens:
            if (in_price is not None and tokens_in is None) or (
                out_price is not None and tokens_out is None
            ):
                continue
            spend += (tokens_in or 0) * (in_price or 0.0) / 1e6
            spend += (tokens_out or 0) * (out_price or 0.0) / 1e6
        spend += per_request
        costed += 1
        costed_ok += 1 if row.get("success") else 0
    if costed == 0:
        return {"status": "no_usage", "requests": total}
    return {
        "status": "computed" if costed == total else "partial_usage",
        "requests": total,
        "requests_costed": costed,
        "total_usd": round(spend, 6),
        "per_1000_decisions_usd": round(spend / costed * 1000, 6),
        "per_1000_successful_decisions_usd": (
            round(spend / costed_ok * 1000, 6) if costed_ok else None
        ),
        "price_source": entry.get("source"),
        "price_retrieved_on": entry.get("retrieved_on"),
        "billing_note": entry.get("notes"),
    }
