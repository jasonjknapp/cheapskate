# SPDX-License-Identifier: Apache-2.0
"""Budget governor: per-user monthly cloud-spend caps that auto-tighten the dial.

Given a user's ``monthly_budget_usd`` (from their :class:`UserProfile` quota) and
their month-to-date cloud spend (derived from telemetry), the governor decides
whether to tighten the spend dial toward local:

  * spend ≥ 80% of the cap  → tighten the dial ONE level toward local
                              (e.g. 0→1, 1→2, 2→3; a level-2 sub-dial steps
                              max→std→lite before the level drops).
  * spend ≥ 95% of the cap  → force LOCAL-ONLY (level 3) for that user's
                              cloud-routable work.

It emits a ``kind="budget_governor"`` telemetry event on each threshold CROSSING
and is idempotent: the same threshold crossing never emits twice. Idempotency is
tracked in a small per-user state file (``state_dir()/governor-<user>.json``),
keyed by ``(month, threshold)`` so a new month resets cleanly.

Cost of the month-to-date cloud spend is computed with the same honest math as
the report (escalations count as cloud spend).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .. import paths, telemetry
from ..config import Config
from . import costmath, pricing
from .report import _GENERATION_KINDS, iter_events

# Thresholds as fractions of the cap. Order matters — highest first when deciding.
_THRESHOLD_TIGHTEN = 0.80
_THRESHOLD_FORCE_LOCAL = 0.95

# Dial sub-steps at level 2, from loosest to tightest, before the level drops.
_SUB_ORDER = ("max", "std", "lite")


@dataclass(frozen=True)
class GovernorDecision:
    """What the governor concluded for a user this run."""

    user: str
    month: str
    budget_usd: float | None
    spend_usd: float
    fraction: float | None  # spend / budget, None when no budget set
    threshold_crossed: str | None  # "tighten" | "force-local" | None
    from_dial: tuple[int, str | None]
    to_dial: tuple[int, str | None]
    changed: bool
    emitted_event: bool
    reason: str


# ── spend from telemetry ─────────────────────────────────────────────────────


def _event_month(evt: dict[str, Any]) -> str | None:
    ts = evt.get("ts")
    return ts[:7] if isinstance(ts, str) and len(ts) >= 7 else None


def month_to_date_cloud_spend(
    events: Iterable[dict[str, Any]],
    snapshot: pricing.PricingSnapshot,
    *,
    user: str,
    month: str,
    cloud_ref_model: str = "gpt-5.4-mini",
) -> float:
    """Sum the USD a user sent to the cloud this month. A generation event counts
    as cloud spend when it was cloud-routed OR escalated off a local attempt.
    Priced at the reference model's token rates (same yardstick as the report)."""
    ref = pricing.lookup(snapshot, cloud_ref_model)
    if ref is None:
        return 0.0
    total = 0.0
    for evt in events:
        if evt.get("kind") not in _GENERATION_KINDS:
            continue
        if evt.get("user") != user:
            continue
        if _event_month(evt) != month:
            continue
        hit_cloud = evt.get("route") == "cloud" or bool(evt.get("escalated"))
        if not hit_cloud:
            continue
        total += costmath.cloud_cost_usd(
            int(evt.get("tokens_in") or 0),
            int(evt.get("tokens_out") or 0),
            ref.input_usd_per_mtok,
            ref.output_usd_per_mtok,
        )
    return total


# ── dial tightening ──────────────────────────────────────────────────────────


def tighten_one_level(dial: tuple[int, str | None]) -> tuple[int, str | None]:
    """Step the dial one notch toward local. At level 2 the sub-dial tightens
    (max→std→lite) before the level advances to 3 (local-only). At level 3 it is
    already local-only — unchanged."""
    level, sub = dial
    if level < 2:
        return (level + 1, None)
    if level == 2:
        cur = sub if sub in _SUB_ORDER else "std"
        idx = _SUB_ORDER.index(cur)
        if idx < len(_SUB_ORDER) - 1:
            return (2, _SUB_ORDER[idx + 1])
        return (3, None)  # past 'lite' → local-only
    return (3, None)  # already local-only


def force_local_only(_dial: tuple[int, str | None]) -> tuple[int, str | None]:
    return (3, None)


# ── idempotency state ────────────────────────────────────────────────────────


def _state_path(user: str) -> Path:
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in user) or "user"
    return paths.state_dir() / f"governor-{safe}.json"


def _load_state(user: str, path: Path | None = None) -> dict[str, Any]:
    p = path if path is not None else _state_path(user)
    try:
        obj = json.loads(p.read_text())
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(user: str, state: dict[str, Any], path: Path | None = None) -> None:
    p = path if path is not None else _state_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state))


def _already_fired(state: dict[str, Any], month: str, threshold: str) -> bool:
    return state.get("month") == month and threshold in state.get("fired", [])


def _record_fired(state: dict[str, Any], month: str, threshold: str) -> dict[str, Any]:
    if state.get("month") != month:
        state = {"month": month, "fired": []}
    fired = list(state.get("fired", []))
    if threshold not in fired:
        fired.append(threshold)
    return {"month": month, "fired": fired}


# ── the governor ─────────────────────────────────────────────────────────────


def govern_user(
    config: Config,
    user: str,
    current_dial: tuple[int, str | None],
    *,
    events: Iterable[dict[str, Any]] | None = None,
    snapshot: pricing.PricingSnapshot | None = None,
    month: str | None = None,
    cloud_ref_model: str = "gpt-5.4-mini",
    state_path: Path | None = None,
    log_event: Callable[..., None] | None = None,
    telemetry_path: Path | None = None,
) -> GovernorDecision:
    """Evaluate one user against their monthly cap and decide the dial.

    Pure-ish: all I/O sources are injectable (``events``, ``snapshot``,
    ``state_path``, ``log_event``) so tests never touch the network, a live
    telemetry writer, or a real clock. Emits at most ONE telemetry event per
    call, and never re-emits a threshold already fired this month (idempotent).

    Returns a :class:`GovernorDecision` describing the outcome; the caller is
    responsible for actually writing the new dial (the governor recommends and
    records, it does not write the dial state file itself)."""
    m = month or datetime.now(timezone.utc).strftime("%Y-%m")
    profile = config.users.get(user)
    budget = profile.quota.monthly_budget_usd if profile else None

    if budget is None or budget <= 0:
        return GovernorDecision(
            user=user, month=m, budget_usd=budget, spend_usd=0.0, fraction=None,
            threshold_crossed=None, from_dial=current_dial, to_dial=current_dial,
            changed=False, emitted_event=False,
            reason="no monthly_budget_usd set for user; governor inactive",
        )

    snap = snapshot if snapshot is not None else pricing.load_pricing()
    evts = list(events) if events is not None else list(iter_events(telemetry_path))
    spend = month_to_date_cloud_spend(
        evts, snap, user=user, month=m, cloud_ref_model=cloud_ref_model
    )
    fraction = spend / budget

    # Decide the highest threshold crossed.
    if fraction >= _THRESHOLD_FORCE_LOCAL:
        threshold = "force-local"
        target = force_local_only(current_dial)
    elif fraction >= _THRESHOLD_TIGHTEN:
        threshold = "tighten"
        target = tighten_one_level(current_dial)
    else:
        return GovernorDecision(
            user=user, month=m, budget_usd=budget, spend_usd=round(spend, 4),
            fraction=round(fraction, 4), threshold_crossed=None,
            from_dial=current_dial, to_dial=current_dial, changed=False,
            emitted_event=False,
            reason=f"spend {fraction:.0%} of cap; below the 80% tighten threshold",
        )

    state = _load_state(user, state_path)
    already = _already_fired(state, m, threshold)
    changed = target != current_dial
    emitter = log_event if log_event is not None else telemetry.log_event

    emitted = False
    if not already:
        emitter(
            "budget_governor",
            user=user,
            task_type="budget",
            route="policy",
            ok=True,
            escalated=False,
            retries=0,
        )
        state = _record_fired(state, m, threshold)
        _save_state(user, state, state_path)
        emitted = True

    return GovernorDecision(
        user=user, month=m, budget_usd=budget, spend_usd=round(spend, 4),
        fraction=round(fraction, 4), threshold_crossed=threshold,
        from_dial=current_dial, to_dial=target, changed=changed,
        emitted_event=emitted,
        reason=(
            f"spend {fraction:.0%} of ${budget:.2f} cap crossed the "
            f"{threshold} threshold"
            + ("" if not already else " (already fired this month — idempotent, no re-emit)")
        ),
    )
