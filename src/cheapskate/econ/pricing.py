# SPDX-License-Identifier: Apache-2.0
"""Cloud model pricing: load a bundled snapshot, warn (never fail) when stale,
look a model up by id with a clearly-reported fuzzy prefix fallback.

The snapshot ``pricing.json`` ships inside the package and is refreshed weekly
by CI (``.github/workflows/pricing-refresh.yml``) — the CLI NEVER fetches prices
at runtime. A user override at ``config_dir()/pricing.json`` wins if present.

Prices are USD per 1,000,000 tokens (``input_usd_per_mtok`` /
``output_usd_per_mtok``). Every row carries its own ``source`` and ``as_of`` so
staleness and provenance are auditable per model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .. import paths

_BUNDLED = Path(__file__).with_name("pricing.json")


@dataclass(frozen=True)
class PriceRow:
    """One model's list price. ``fuzzy`` is True when this row was returned by a
    prefix match rather than an exact id hit — always reported, never hidden."""

    id: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    source: str
    as_of: str
    fuzzy: bool = False
    matched_id: str | None = None  # the actual row id when fuzzy


@dataclass(frozen=True)
class PricingSnapshot:
    """A loaded pricing table plus where it came from."""

    rows: dict[str, PriceRow]
    origin: str  # "bundled" | "override"
    path: Path

    def newest_as_of(self) -> date | None:
        dates = [_parse_as_of(r.as_of) for r in self.rows.values()]
        dates = [d for d in dates if d is not None]
        return max(dates) if dates else None


def _parse_as_of(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except (ValueError, AttributeError):
        return None


def _override_path() -> Path:
    return paths.config_dir() / "pricing.json"


def _load_file(path: Path) -> dict[str, PriceRow]:
    data: dict[str, Any] = json.loads(path.read_text())
    rows: dict[str, PriceRow] = {}
    for entry in data.get("models", []):
        try:
            row = PriceRow(
                id=str(entry["id"]),
                input_usd_per_mtok=float(entry["input_usd_per_mtok"]),
                output_usd_per_mtok=float(entry["output_usd_per_mtok"]),
                source=str(entry.get("source", "unknown")),
                as_of=str(entry.get("as_of", "")),
            )
        except (KeyError, TypeError, ValueError):
            # A malformed row is skipped, not fatal — the snapshot stays usable.
            continue
        rows[row.id] = row
    return rows


def load_pricing(*, config_dir: Path | None = None) -> PricingSnapshot:
    """Load the effective pricing snapshot: a user override at
    ``config_dir()/pricing.json`` if present and parseable, else the bundled
    snapshot. Never raises on a bad override — it falls back to bundled."""
    override = (config_dir / "pricing.json") if config_dir is not None else _override_path()
    if override.exists():
        try:
            rows = _load_file(override)
            if rows:
                return PricingSnapshot(rows=rows, origin="override", path=override)
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # fall through to bundled
    return PricingSnapshot(rows=_load_file(_BUNDLED), origin="bundled", path=_BUNDLED)


def staleness_days(snapshot: PricingSnapshot, *, today: date | None = None) -> int | None:
    """How many days old the newest row is. None if no row has a parseable date."""
    newest = snapshot.newest_as_of()
    if newest is None:
        return None
    ref = today if today is not None else datetime.now(timezone.utc).date()
    return (ref - newest).days


def staleness_warning(
    snapshot: PricingSnapshot, max_age_days: int, *, today: date | None = None
) -> str | None:
    """Return a human warning string if the snapshot is older than
    ``max_age_days`` (warn, never fail). None when fresh or undatable."""
    days = staleness_days(snapshot, today=today)
    if days is None:
        return "pricing snapshot has no dated rows; age unknown"
    if days > max_age_days:
        return (
            f"pricing snapshot is {days}d old (limit {max_age_days}d) — "
            f"cloud cost figures may be stale; refresh pricing.json"
        )
    return None


def lookup(snapshot: PricingSnapshot, model_id: str) -> PriceRow | None:
    """Look up a model's price. Exact id first; then a fuzzy fallback that
    prefix-matches in EITHER direction (query is a prefix of a row id, or a row
    id is a prefix of the query — e.g. a dated variant ``gpt-5.4-mini-2026-05-01``).
    A fuzzy hit is flagged ``fuzzy=True`` and carries ``matched_id`` so callers
    can surface "matched X to Y (fuzzy)". Ambiguous prefix ⇒ the longest (most
    specific) matched row id wins deterministically. No match ⇒ None."""
    if not model_id:
        return None
    exact = snapshot.rows.get(model_id)
    if exact is not None:
        return exact

    q = model_id.strip()
    candidates = [
        row for rid, row in snapshot.rows.items() if rid.startswith(q) or q.startswith(rid)
    ]
    if not candidates:
        return None
    # Deterministic: prefer the longest matched id, then lexical order.
    best = sorted(candidates, key=lambda r: (-len(r.id), r.id))[0]
    return PriceRow(
        id=best.id,
        input_usd_per_mtok=best.input_usd_per_mtok,
        output_usd_per_mtok=best.output_usd_per_mtok,
        source=best.source,
        as_of=best.as_of,
        fuzzy=True,
        matched_id=best.id,
    )
