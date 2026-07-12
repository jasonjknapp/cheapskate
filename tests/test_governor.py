# SPDX-License-Identifier: Apache-2.0
"""Budget governor: threshold-driven dial tightening + idempotent telemetry.

No network, no live telemetry writer, no real clock — events, pricing snapshot,
state path, and the telemetry emitter are all injected.
"""

from __future__ import annotations

import json

from cheapskate.config import Config, UserProfile, UserQuota
from cheapskate.econ import governor, pricing


def _cfg(budget):
    return Config(
        users={
            "interactive": UserProfile(quota=UserQuota(monthly_budget_usd=budget)),
        }
    )


def _snapshot():
    # A FIXED snapshot decoupled from the shipped catalog: the default cloud
    # reference (gpt-5.4-mini) is pinned here at $0.15 in / $0.60 out so the
    # arithmetic in each test stays valid regardless of real-world price drift.
    rows = {
        "gpt-5.4-mini": pricing.PriceRow("gpt-5.4-mini", 0.15, 0.6, "test-fixed", "2026-07-11"),
    }
    return pricing.PricingSnapshot(rows=rows, origin="bundled", path=pricing._BUNDLED)


def _cloud_event(tokens_in, tokens_out, *, ts="2026-07-05T00:00:00+00:00", user="interactive"):
    return {
        "ts": ts,
        "kind": "generation",
        "task_type": "review",
        "user": user,
        "route": "cloud",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "retries": 0,
        "escalated": False,
        "ok": True,
    }


# ── dial tightening mechanics ────────────────────────────────────────────────


def test_tighten_one_level_below_two():
    assert governor.tighten_one_level((0, None)) == (1, None)
    assert governor.tighten_one_level((1, None)) == (2, None)


def test_tighten_steps_sub_dial_before_level():
    assert governor.tighten_one_level((2, "max")) == (2, "std")
    assert governor.tighten_one_level((2, "std")) == (2, "lite")
    assert governor.tighten_one_level((2, "lite")) == (3, None)


def test_tighten_at_local_only_is_noop():
    assert governor.tighten_one_level((3, None)) == (3, None)


def test_force_local_only():
    assert governor.force_local_only((0, None)) == (3, None)
    assert governor.force_local_only((2, "max")) == (3, None)


# ── spend computation ────────────────────────────────────────────────────────


def test_month_to_date_spend_counts_cloud_and_escalations():
    # price at gpt-4o-mini: in $0.15/Mtok, out $0.6/Mtok
    events = [
        _cloud_event(1_000_000, 1_000_000),  # $0.15 + $0.60 = $0.75
        {  # a local run that escalated → counts as cloud spend
            "ts": "2026-07-06T00:00:00+00:00", "kind": "generation", "user": "interactive",
            "route": "local", "escalated": True, "tokens_in": 1_000_000,
            "tokens_out": 0, "ok": False, "task_type": "review",
        },  # $0.15
        {  # a pure local run → NOT counted
            "ts": "2026-07-06T00:00:00+00:00", "kind": "generation", "user": "interactive",
            "route": "local", "escalated": False, "tokens_in": 1_000_000,
            "tokens_out": 1_000_000, "ok": True, "task_type": "review",
        },
    ]
    spend = governor.month_to_date_cloud_spend(
        events, _snapshot(), user="interactive", month="2026-07"
    )
    assert abs(spend - 0.90) < 1e-6  # 0.75 + 0.15


def test_spend_filters_by_user_and_month():
    events = [
        _cloud_event(1_000_000, 0, user="other"),  # wrong user
        _cloud_event(1_000_000, 0, ts="2026-06-01T00:00:00+00:00"),  # wrong month
        _cloud_event(1_000_000, 0),  # counts: $0.15
    ]
    spend = governor.month_to_date_cloud_spend(
        events, _snapshot(), user="interactive", month="2026-07"
    )
    assert abs(spend - 0.15) < 1e-6


# ── governor decisions ───────────────────────────────────────────────────────


def test_inactive_when_no_budget(tmp_path):
    cfg = _cfg(None)
    dec = governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[], snapshot=_snapshot(),
        month="2026-07", state_path=tmp_path / "gov.json",
        log_event=lambda *a, **k: None,
    )
    assert dec.threshold_crossed is None
    assert dec.changed is False
    assert "inactive" in dec.reason


def test_below_threshold_no_change(tmp_path):
    # budget $10, spend $0.15 → 1.5% → below 80%
    cfg = _cfg(10.0)
    dec = governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[_cloud_event(1_000_000, 0)], snapshot=_snapshot(),
        month="2026-07", state_path=tmp_path / "gov.json",
        log_event=lambda *a, **k: None,
    )
    assert dec.threshold_crossed is None
    assert dec.to_dial == (2, "std")


def test_tighten_at_80_percent(tmp_path):
    # budget $1.00; spend $0.90 (from the combined event above) → 90%? Use exact:
    # 1M in + 1M out at gpt-4o-mini = $0.75 → 75% (below). Bump to hit 80-95%.
    # in 4M out 1M = 4*0.15 + 0.6 = 1.2 ... too high. Aim ~0.85:
    # in 1M out 1.166M → 0.15 + 0.7 = 0.85 → 85% of $1
    events = [_cloud_event(1_000_000, 1_166_667)]
    cfg = _cfg(1.0)
    emitted = []
    dec = governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=events, snapshot=_snapshot(),
        month="2026-07", state_path=tmp_path / "gov.json",
        log_event=lambda kind, **k: emitted.append((kind, k)),
    )
    assert 0.80 <= dec.fraction < 0.95
    assert dec.threshold_crossed == "tighten"
    assert dec.to_dial == (2, "lite")  # std → lite (one step toward local)
    assert dec.emitted_event is True
    assert emitted and emitted[0][0] == "budget_governor"


def test_force_local_at_95_percent(tmp_path):
    # spend ≥ 95% of $1 → force local-only. in 6M out 0 = $0.90 = 90%? need ≥0.95.
    # in 6M out 1M = 0.90 + 0.6 = 1.5 → 150% ≥ 95%
    events = [_cloud_event(6_000_000, 1_000_000)]
    cfg = _cfg(1.0)
    dec = governor.govern_user(
        cfg, "interactive", (1, None),
        events=events, snapshot=_snapshot(),
        month="2026-07", state_path=tmp_path / "gov.json",
        log_event=lambda *a, **k: None,
    )
    assert dec.fraction >= 0.95
    assert dec.threshold_crossed == "force-local"
    assert dec.to_dial == (3, None)


def test_idempotent_no_repeat_emit_same_threshold(tmp_path):
    events = [_cloud_event(1_000_000, 1_166_667)]  # ~85% of $1 → tighten
    cfg = _cfg(1.0)
    state = tmp_path / "gov.json"
    emitted = []
    logger = lambda kind, **k: emitted.append(kind)  # noqa: E731

    d1 = governor.govern_user(
        cfg, "interactive", (2, "std"), events=events, snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=logger,
    )
    d2 = governor.govern_user(
        cfg, "interactive", (2, "std"), events=events, snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=logger,
    )
    assert d1.emitted_event is True
    assert d2.emitted_event is False  # same threshold, same month → no re-emit
    assert emitted.count("budget_governor") == 1
    # the recommendation is still surfaced even when not re-emitting
    assert d2.threshold_crossed == "tighten"


def test_new_month_resets_idempotency(tmp_path):
    events_jul = [_cloud_event(1_000_000, 1_166_667, ts="2026-07-05T00:00:00+00:00")]
    events_aug = [_cloud_event(1_000_000, 1_166_667, ts="2026-08-05T00:00:00+00:00")]
    cfg = _cfg(1.0)
    state = tmp_path / "gov.json"
    emitted = []
    logger = lambda kind, **k: emitted.append(kind)  # noqa: E731

    governor.govern_user(
        cfg, "interactive", (2, "std"), events=events_jul, snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=logger,
    )
    governor.govern_user(
        cfg, "interactive", (2, "std"), events=events_aug, snapshot=_snapshot(),
        month="2026-08", state_path=state, log_event=logger,
    )
    assert emitted.count("budget_governor") == 2  # each month fires once


def test_escalation_from_tighten_to_force_local_emits_again(tmp_path):
    cfg = _cfg(1.0)
    state = tmp_path / "gov.json"
    emitted = []
    logger = lambda kind, **k: emitted.append(kind)  # noqa: E731

    # first: 85% → tighten
    governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[_cloud_event(1_000_000, 1_166_667)], snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=logger,
    )
    # later: 150% → force-local (a DIFFERENT threshold → emits)
    dec = governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[_cloud_event(6_000_000, 1_000_000)], snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=logger,
    )
    assert dec.threshold_crossed == "force-local"
    assert dec.emitted_event is True
    assert emitted.count("budget_governor") == 2


def test_governor_state_file_shape(tmp_path):
    state = tmp_path / "gov.json"
    cfg = _cfg(1.0)
    governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[_cloud_event(1_000_000, 1_166_667)], snapshot=_snapshot(),
        month="2026-07", state_path=state, log_event=lambda *a, **k: None,
    )
    saved = json.loads(state.read_text())
    assert saved["month"] == "2026-07"
    assert "tighten" in saved["fired"]


def test_governor_telemetry_event_is_content_free(tmp_path):
    """The event the governor emits must only carry content-free fields."""
    cfg = _cfg(1.0)
    captured = {}
    def logger(kind, **fields):
        captured["kind"] = kind
        captured["fields"] = fields
    governor.govern_user(
        cfg, "interactive", (2, "std"),
        events=[_cloud_event(1_000_000, 1_166_667)], snapshot=_snapshot(),
        month="2026-07", state_path=tmp_path / "gov.json", log_event=logger,
    )
    forbidden = {"prompt", "output", "content", "text", "messages", "payload"}
    assert not (set(captured["fields"]) & forbidden)
    assert captured["kind"] == "budget_governor"
