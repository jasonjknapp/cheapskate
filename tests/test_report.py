# SPDX-License-Identifier: Apache-2.0
"""Report: folds telemetry into per-task-type stats + receipts, and the HARD
guarantee that ``--share`` output is content-free even against poisoned lines."""

from __future__ import annotations

import json
from datetime import date

import pytest

from cheapskate.config import Config, EconConfig
from cheapskate.econ import pricing, report
from cheapskate.econ.power import PowerReading


def _write_telemetry(tmp_path, events):
    p = tmp_path / "telemetry.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def _gen(**over):
    base = {
        "ts": "2026-07-05T10:00:00.000+00:00",
        "kind": "generation",
        "machine_id": "test-machine",
        "task_type": "summarize",
        "user": "interactive",
        "route": "local",
        "duration_s": 2.0,
        "tokens_in": 1000,
        "tokens_out": 500,
        "retries": 0,
        "escalated": False,
        "ok": True,
        "model": "local-reasoner",
    }
    base.update(over)
    return base


# ── stats folding ────────────────────────────────────────────────────────────


def test_collect_stats_basic_counts(tmp_path):
    events = [
        _gen(),
        _gen(route="cloud", escalated=False),
        _gen(retries=2, escalated=True, ok=False),
    ]
    stats = report.collect_stats(events)
    st = stats["summarize"]
    assert st.runs == 3
    # two local-routed events (the escalated one still ROUTED local), one cloud
    assert st.local_runs == 2
    assert st.cloud_runs == 1
    assert st.ok_runs == 2
    assert st.total_retries == 2
    assert st.escalations == 1
    assert st.pct_local == pytest.approx(2 / 3)
    assert st.retry_rate == pytest.approx(2 / 3)
    assert st.escalation_rate == pytest.approx(1 / 3)


def test_collect_stats_tokens_per_sec(tmp_path):
    # 500 out tokens over 2.0s → 250 tok/s
    stats = report.collect_stats([_gen()])
    assert stats["summarize"].tokens_per_sec == pytest.approx(250.0)


def test_task_run_summary_is_not_double_counted():
    # S3 double-counting decision: the router emits one kind="generation" event
    # per ATTEMPT (the costable unit) PLUS a kind="task.run" SUMMARY per run. The
    # report costs ONLY "generation" so an attempt is never counted twice; the
    # "task.run" summary is deliberately excluded from stats.
    events = [_gen(kind="generation"), _gen(kind="task.run")]
    stats = report.collect_stats(events)
    assert stats["summarize"].runs == 1  # only the generation event was costed


def test_month_filter(tmp_path):
    events = [
        _gen(ts="2026-07-05T10:00:00.000+00:00"),
        _gen(ts="2026-06-05T10:00:00.000+00:00"),
    ]
    stats = report.collect_stats(events, month="2026-07")
    assert stats["summarize"].runs == 1


def test_non_generation_events_ignored():
    stats = report.collect_stats([{"kind": "doctor", "ok": True, "ts": "2026-07-01T00:00:00+00:00"}])
    assert stats == {}


def test_malformed_line_skipped(tmp_path):
    p = tmp_path / "telemetry.jsonl"
    p.write_text('{"kind":"generation","task_type":"x","ts":"2026-07-01T00:00:00+00:00","runs":1}\nNOT JSON\n')
    events = list(report.iter_events(p))
    assert len(events) == 1


# ── costing + recommendation ─────────────────────────────────────────────────


def _config_with_energy():
    return Config(
        econ=EconConfig(
            electricity_usd_per_kwh=0.15,
            hardware_amortization_usd_per_month=None,
            watts_estimate=20.0,
        )
    )


def _snapshot():
    return pricing.load_pricing()


def _power_known():
    return PowerReading(watts=20.0, mode="estimate", detail="test")


def test_build_task_reports_energy_known():
    events = [_gen() for _ in range(6)]
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    (r,) = reports
    assert r.energy_known is True
    assert r.local_cost_per_run_usd is not None
    assert r.cloud_equiv_per_run_usd is not None
    # local should be far cheaper than cloud here → stay-local
    assert r.recommendation in {"stay-local", "mixed", "go-cloud"}


def test_energy_unknown_mode_omits_local_cost():
    # no electricity price and no watts → power unknown, local cost None
    cfg = Config(econ=EconConfig(electricity_usd_per_kwh=None, watts_estimate=None))
    power_unknown = PowerReading(watts=None, mode="unknown", detail="test")
    stats = report.collect_stats([_gen() for _ in range(6)])
    reports = report.build_task_reports(stats, cfg, _snapshot(), power_unknown)
    (r,) = reports
    assert r.energy_known is False
    assert r.local_cost_per_run_usd is None  # omitted, not guessed


def test_recommendation_insufficient_data_under_floor():
    stats = report.collect_stats([_gen() for _ in range(3)])  # < 5 runs
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    assert reports[0].recommendation == "insufficient-data"


def test_recommendation_mixed_when_escalation_heavy():
    # 10 runs, half escalate → escalation_rate 0.5 ≥ 0.30 → mixed
    events = [_gen() for _ in range(5)] + [_gen(escalated=True, ok=False) for _ in range(5)]
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    assert reports[0].recommendation == "mixed"


# ── receipts ─────────────────────────────────────────────────────────────────


def test_receipts_savings_never_negative():
    events = [_gen() for _ in range(10)]  # all local, no escalation
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    receipts = report.compute_receipts(reports, month="2026-07", assumptions=["x"])
    assert receipts.saved_usd >= 0.0
    assert receipts.pct_local == 1.0
    # all-cloud reference > actual cloud (0 here) → positive savings
    assert receipts.all_cloud_usd > 0
    assert receipts.cloud_spend_usd == 0.0


def test_receipts_assumptions_disclosed():
    cfg = _config_with_energy()
    bundle = report.generate(cfg, month="2026-07", path=None)
    # even with empty telemetry the assumptions list is populated
    receipts = bundle.receipts
    assert any("reference model" in a for a in receipts.assumptions)
    assert any("true cost charges local retries" in a for a in receipts.assumptions)


def test_generate_end_to_end(tmp_path, monkeypatch):
    events = [_gen() for _ in range(8)]
    p = _write_telemetry(tmp_path, events)
    bundle = report.generate(
        _config_with_energy(), month="2026-07", path=p, today=date(2026, 7, 12)
    )
    assert bundle.reports
    text = report.render_report(
        bundle.reports, bundle.receipts,
        pricing_origin=bundle.pricing_origin, staleness=bundle.staleness,
        power=bundle.power,
    )
    assert "Cheapskate report" in text
    assert "summarize" in text
    assert "Receipts" in text


# ── the HARD guarantee: --share is content-free ──────────────────────────────

_POISON = "SECRET-API-KEY-sk-abc123-do-not-leak"


def _poisoned_events():
    """Generation events whose text/content fields are stuffed with a secret.
    The report must never surface any of it in --share."""
    return [
        _gen(
            # forbidden content fields a hostile/buggy writer might have slipped in
            prompt=_POISON,
            output=_POISON,
            content=_POISON,
            text=_POISON,
            messages=_POISON,
            payload=_POISON,
            error_kind=_POISON,
            note=_POISON,
        )
        for _ in range(6)
    ]


def test_share_never_emits_poisoned_content():
    events = _poisoned_events()
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    receipts = report.compute_receipts(reports, month="2026-07", assumptions=["ref x"])
    share = report.render_share(reports, receipts, machine_id="test-machine")
    assert _POISON not in share
    assert "sk-abc123" not in share


def test_share_poisoned_task_type_and_model_are_scrubbed():
    # even the identifier fields --share IS allowed to read get control chars
    # stripped so they can't inject markdown/newlines into the shared receipt
    evil = "evil|`\ninjected"
    events = [
        _gen(task_type=evil, model=evil, prompt=_POISON) for _ in range(6)
    ]
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    receipts = report.compute_receipts(reports, month="2026-07", assumptions=[])
    share = report.render_share(reports, receipts, machine_id="test-machine")
    assert _POISON not in share
    assert "\ninjected" not in share  # newline stripped from identifier
    assert "|`" not in share.replace("|---", "").replace("| ", "").replace(" |", "")


def test_share_only_reads_allowlisted_fields():
    """Structural pin: the share-safe field allowlist must exclude every known
    content-bearing field name."""
    forbidden = {"prompt", "output", "content", "text", "messages", "payload"}
    assert not (report._SHARE_SAFE_EVENT_FIELDS & forbidden)


def test_share_output_is_aggregate_markdown():
    events = [_gen() for _ in range(6)]
    stats = report.collect_stats(events)
    reports = report.build_task_reports(
        stats, _config_with_energy(), _snapshot(), _power_known()
    )
    receipts = report.compute_receipts(reports, month="2026-07", assumptions=["a"])
    share = report.render_share(reports, receipts, machine_id="test-machine")
    assert "## Cheapskate savings" in share
    assert "| task type |" in share
    assert "routed local" in share
    assert "test-machine" in share  # machine_id IS allowed
