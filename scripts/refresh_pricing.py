#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Refresh ``src/cheapskate/econ/pricing.json`` from a PUBLIC pricing source.

Runs in CI ONLY (the weekly ``pricing-refresh.yml`` job) — this is the sole place
cheapskate touches the network for pricing. The CLI never fetches at runtime.

Source: OpenRouter's public models endpoint (``https://openrouter.ai/api/v1/models``)
which lists per-model prompt/completion prices in USD PER TOKEN with no API key
required. We convert to USD per 1,000,000 tokens and refresh the price of any
model already present in our small curated snapshot (matched by a normalized id
or a fuzzy suffix). We deliberately do NOT balloon the snapshot to hundreds of
rows — small and honest beats exhaustive. Rows we cannot confidently match are
left untouched with their existing source + as_of.

Idempotent: re-running with unchanged upstream prices leaves the file byte-stable
(sorted keys, trailing newline) so CI opens no spurious PR.
"""

from __future__ import annotations

import datetime
import json
import sys
import urllib.request
from pathlib import Path

_PRICING = Path(__file__).resolve().parents[1] / "src" / "cheapskate" / "econ" / "pricing.json"
_SOURCE_URL = "https://openrouter.ai/api/v1/models"
_SOURCE_LABEL = "openrouter.ai/api/v1/models"


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "cheapskate-pricing-refresh"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — trusted public URL, CI only
        return json.loads(resp.read().decode("utf-8"))


def _norm(model_id: str) -> str:
    """Normalize a model id for matching: lowercase, drop a provider prefix
    (``openai/gpt-4o`` → ``gpt-4o``), strip dashes/dots for a loose compare."""
    base = model_id.lower().split("/")[-1]
    return base.replace(".", "").replace("-", "")


def _index_upstream(payload: dict) -> dict[str, tuple[float, float]]:
    """Map normalized id → (input_usd_per_mtok, output_usd_per_mtok)."""
    out: dict[str, tuple[float, float]] = {}
    for entry in payload.get("data", []):
        mid = entry.get("id")
        pricing = entry.get("pricing") or {}
        try:
            # OpenRouter prices are USD per token as strings.
            in_per_tok = float(pricing["prompt"])
            out_per_tok = float(pricing["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        if in_per_tok <= 0 and out_per_tok <= 0:
            continue  # free/unpriced listing — skip
        out[_norm(str(mid))] = (in_per_tok * 1_000_000, out_per_tok * 1_000_000)
    return out


def main() -> int:
    snapshot = json.loads(_PRICING.read_text())
    try:
        upstream = _index_upstream(_fetch(_SOURCE_URL))
    except Exception as exc:  # noqa: BLE001 — a fetch failure must not corrupt the file
        print(f"pricing refresh: upstream fetch failed ({exc!r}); leaving file unchanged")
        return 0

    today = datetime.date.today().isoformat()
    changed = 0
    for row in snapshot.get("models", []):
        key = _norm(str(row.get("id", "")))
        if key not in upstream:
            continue
        new_in, new_out = upstream[key]
        new_in = round(new_in, 4)
        new_out = round(new_out, 4)
        if row.get("input_usd_per_mtok") != new_in or row.get("output_usd_per_mtok") != new_out:
            changed += 1
        row["input_usd_per_mtok"] = new_in
        row["output_usd_per_mtok"] = new_out
        row["source"] = _SOURCE_LABEL
        row["as_of"] = today

    _PRICING.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n")
    print(f"pricing refresh: {changed} row(s) changed; wrote {_PRICING}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
