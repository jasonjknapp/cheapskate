# SPDX-License-Identifier: Apache-2.0
"""Router S3 wiring: cloud dispatch in the LIVE path, fail-closed both
directions, budget-governor tightening, and per-attempt generation telemetry.
Everything is injected — no network, no live servers, no real keys."""

from __future__ import annotations

import json

import pytest

from cheapskate.config import Config, ProviderConfig, UserProfile, UserQuota
from cheapskate.econ import governor as _governor
from cheapskate.router import task


@pytest.fixture
def events(monkeypatch):
    """Capture telemetry events emitted by the router."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(task.telemetry, "log_event",
                        lambda kind, **f: captured.append((kind, f)))
    return captured


def _envelope(output, met=True, conf=0.9):
    return json.dumps({"output": output, "self_confidence": conf, "criteria_met": met})


class _CloudResult:
    def __init__(self, text, model="cloud-m", tin=10, tout=20):
        self.text, self.model, self.tokens_in, self.tokens_out = text, model, tin, tout


def _no_govern(*a, **k):
    return None  # governor recommends no change


# ── FAIL CLOSED: never_local ─────────────────────────────────────────────────


def test_never_local_raises_no_local_no_cloud(events):
    cfg = Config(never_local=["financial"])
    local_calls, cloud_calls = [], []
    with pytest.raises(task.NeverLocal):
        task.run("financial", "c", "d", cfg,
                 complete=lambda *a, **k: local_calls.append(1) or "x",
                 cloud_dispatch=lambda *a, **k: cloud_calls.append(1))
    assert not local_calls  # no local answer
    assert not cloud_calls  # AND no silent cloud fallback


# ── FAIL CLOSED: never_cloud + local impossible ──────────────────────────────


def test_never_cloud_at_level_0_refuses_not_ships(events):
    # never_cloud at dial level 0 (cloud-first): must NOT ship off-box → NeverCloud
    cfg = Config(never_cloud=["secrets"])
    cloud_calls = []
    with pytest.raises(task.NeverCloud):
        task.run("secrets", "c", "d", cfg, dial=(0, None),
                 cloud_dispatch=lambda *a, **k: cloud_calls.append(1),
                 complete=lambda *a, **k: _envelope("x"))
    assert not cloud_calls  # never dispatched to the cloud


def test_never_cloud_forced_local_runs_local(events):
    # never_cloud at any local-capable dial stays local (forced), verify+run
    cfg = Config(never_cloud=["secrets"])
    res = task.run("secrets", "c", "d", cfg, dial=(2, "std"),
                   complete=lambda *a, **k: _envelope("kept local"),
                   verify=lambda o, c: (True, ""))
    assert res["route"] == "local"
    assert res["output"] == "kept local"


# ── FAIL CLOSED: cloud route, no enabled provider ────────────────────────────


def test_cloud_route_no_provider_hard_errors(events):
    # dial 0 sends summarize cloud; default config has no enabled provider →
    # CloudUnavailable, never a local fallback.
    cfg = Config()
    local_calls = []
    with pytest.raises(task.CloudUnavailable) as e:
        task.run("summarize", "c", "d", cfg, dial=(0, None),
                 complete=lambda *a, **k: local_calls.append(1) or "x",
                 govern=_no_govern)
    assert "provider" in str(e.value).lower()
    assert not local_calls


# ── cloud dispatch happy path (LIVE) ─────────────────────────────────────────


def _cloud_cfg():
    return Config(providers={
        "cloud": ProviderConfig(
            kind="openai-compat", base_url="https://x/v1",
            model_map={"reasoning": "gpt-x"}, api_key_env="K", enabled=True,
        )
    })


def test_cloud_route_dispatches_and_returns_output(events):
    cfg = _cloud_cfg()
    seen = []

    def dispatch(config, role, prompt, system=None):
        seen.append((role, prompt, system))
        return _CloudResult(_envelope("cloud output"), model="gpt-x", tin=12, tout=34)

    res = task.run("summarize", "c", "d", cfg, dial=(0, None),
                   cloud_dispatch=dispatch, verify=lambda o, c: (True, ""),
                   govern=_no_govern)
    assert res["route"] == "cloud"
    assert res["output"] == "cloud output"
    assert res["model"] == "gpt-x"
    assert res["tokens_in"] == 12
    assert res["tokens_out"] == 34
    assert seen and seen[0][0] == "reasoning"


def test_cloud_dispatch_failure_is_hard_error(events):
    cfg = _cloud_cfg()

    def dispatch(config, role, prompt, system=None):
        raise task.CloudUnavailable("provider exploded")

    with pytest.raises(task.CloudUnavailable):
        task.run("summarize", "c", "d", cfg, dial=(0, None),
                 cloud_dispatch=dispatch, govern=_no_govern)


# ── budget governor wiring ───────────────────────────────────────────────────


def test_governor_over_budget_runs_local_instead_of_cloud(events):
    # A cloud-routable task; the governor forces local (dial 3) for this request.
    cfg = _cloud_cfg()
    local_calls, cloud_calls = [], []

    class _Decision:
        to_dial = (3, None)  # governor forces local-only

    res = task.run(
        "summarize", "c", "d", cfg, dial=(0, None),
        complete=lambda *a, **k: local_calls.append(1) or _envelope("local win"),
        cloud_dispatch=lambda *a, **k: cloud_calls.append(1),
        verify=lambda o, c: (True, ""),
        govern=lambda config, user, dial: _Decision(),
    )
    assert res["route"] == "local"
    assert res["output"] == "local win"
    assert cloud_calls == []  # governor kept it off the cloud
    assert local_calls  # it ran locally


def test_governor_at_95_percent_forces_local(events, tmp_path):
    # End-to-end with the real governor: user at >=95% of a $1 cap.
    cfg = Config(
        providers=_cloud_cfg().providers,
        users={"interactive": UserProfile(quota=UserQuota(monthly_budget_usd=1.0))},
    )
    # 6M in + 1M out at the default gpt-5.4-mini reference far exceeds the $1 cap
    # → well above 95% → force local (the exact multiple is not asserted)
    from cheapskate.econ import pricing

    spend_events = [{
        "ts": "2026-07-05T00:00:00+00:00", "kind": "generation", "user": "interactive",
        "route": "cloud", "tokens_in": 6_000_000, "tokens_out": 1_000_000,
        "escalated": False, "ok": True, "task_type": "summarize",
    }]

    def govern(config, user, dial):
        return _governor.govern_user(
            config, user, dial, events=spend_events, snapshot=pricing.load_pricing(),
            month="2026-07", state_path=tmp_path / "gov.json",
            log_event=lambda *a, **k: None,
        )

    cloud_calls = []
    res = task.run(
        "summarize", "c", "d", cfg, dial=(0, None),
        complete=lambda *a, **k: _envelope("forced local"),
        cloud_dispatch=lambda *a, **k: cloud_calls.append(1),
        verify=lambda o, c: (True, ""),
        govern=govern, user="interactive",
    )
    assert res["route"] == "local"
    assert cloud_calls == []


# ── telemetry convergence: generation-per-attempt + task.run summary ─────────


def test_local_emits_generation_per_attempt_plus_summary(events):
    cfg = Config()
    calls = {"n": 0}

    def verify(o, c):
        calls["n"] += 1
        return (calls["n"] >= 2, "fix it")  # fail once, then accept → 2 attempts

    task.run("summarize", "c", "d", cfg, dial=(2, "std"),
             complete=lambda *a, **k: _envelope("x"), verify=verify)
    gens = [f for k, f in events if k == "generation"]
    runs = [f for k, f in events if k == "task.run"]
    assert len(gens) == 2  # one generation event per attempt
    assert len(runs) == 1  # exactly one summary
    # per-attempt retries index: 0 then 1
    assert [g["retries"] for g in gens] == [0, 1]
    assert runs[0]["retries"] == 1  # summary reports the repair count


def test_cloud_emits_generation_per_attempt_with_tokens(events):
    cfg = _cloud_cfg()

    def dispatch(config, role, prompt, system=None):
        return _CloudResult(_envelope("ok"), model="gpt-x", tin=7, tout=8)

    task.run("summarize", "c", "d", cfg, dial=(0, None),
             cloud_dispatch=dispatch, verify=lambda o, c: (True, ""),
             govern=_no_govern)
    gens = [f for k, f in events if k == "generation"]
    assert len(gens) == 1
    assert gens[0]["route"] == "cloud"
    assert gens[0]["tokens_in"] == 7
    assert gens[0]["tokens_out"] == 8


def test_generation_events_are_content_free(events):
    cfg = Config()
    task.run("summarize", "c", "d", cfg, dial=(2, "std"),
             complete=lambda *a, **k: _envelope("secret data here"),
             verify=lambda o, c: (True, ""))
    forbidden = {"prompt", "output", "content", "text", "messages", "payload"}
    for kind, fields in events:
        assert not (set(fields) & forbidden), f"{kind} leaked a content field"
