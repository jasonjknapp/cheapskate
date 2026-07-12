# SPDX-License-Identifier: Apache-2.0
"""Task: verify-and-repair (<=2 retries then escalate), injected model call,
fail-closed refusals, no silent cloud fallback."""

from __future__ import annotations

import json

import pytest

from cheapskate.config import Config
from cheapskate.router import task


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch):
    monkeypatch.setattr(task.telemetry, "log_event", lambda *a, **k: None)


def _envelope(output, met=True, conf=0.9):
    return json.dumps({"output": output, "self_confidence": conf, "criteria_met": met})


def test_never_local_raises_no_fallback():
    cfg = Config(never_local=["financial"])
    called = []
    with pytest.raises(task.NeverLocal):
        task.run("financial", "crit", "data", cfg, complete=lambda *a, **k: called.append(1) or "x")
    assert not called  # the model was never called — no silent fallback


def test_never_cloud_refuses_at_level_0():
    cfg = Config(never_cloud=["secrets"])
    with pytest.raises(task.NeverCloud):
        task.run("secrets", "crit", "data", cfg, dial=(0, None), complete=lambda *a, **k: "x")


def test_cloud_route_with_no_enabled_provider_fails_closed():
    # S3: a cloud route (dial level 0) with no enabled provider is a HARD error
    # (CloudUnavailable), never a silent local downgrade. The local model is
    # never called.
    cfg = Config()
    called = []
    with pytest.raises(task.CloudUnavailable) as e:
        task.run(
            "summarize", "crit", "data", cfg, dial=(0, None),
            complete=lambda *a, **k: called.append(1) or "x",
            govern=lambda *a, **k: None,  # no governor tightening
        )
    assert "provider" in str(e.value).lower()  # actionable message
    assert not called  # no silent local fallback


def test_local_success_first_try():
    cfg = Config()
    res = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=lambda *a, **k: _envelope("done"),
        verify=lambda out, crit: (True, ""),
    )
    assert res["route"] == "local"
    assert res["ok"] is True
    assert res["output"] == "done"
    assert res["retries"] == 0
    assert res["escalated"] is False


def test_verify_repair_then_succeed():
    cfg = Config()
    attempts = []

    def complete(prompt, system=None, role=None):
        attempts.append(prompt)
        return _envelope("attempt")

    # fail once, then accept
    calls = {"n": 0}

    def verify(out, crit):
        calls["n"] += 1
        return (calls["n"] >= 2, "needs work")

    res = task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                   complete=complete, verify=verify)
    assert res["ok"] is True
    assert res["retries"] == 1
    assert res["escalated"] is False
    # the repair prompt carried the feedback
    assert "needs work" in attempts[1]


def test_escalates_after_two_retries():
    cfg = Config()
    n = {"complete": 0}

    def complete(prompt, system=None, role=None):
        n["complete"] += 1
        return _envelope("nope")

    res = task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                   complete=complete, verify=lambda out, crit: (False, "still bad"),
                   max_retries=2)
    # 1 initial + 2 retries = 3 model calls, then escalate
    assert n["complete"] == 3
    assert res["retries"] == 2
    assert res["escalated"] is True
    assert res["ok"] is False


def test_model_exception_is_a_repairable_attempt_then_escalates():
    cfg = Config()

    def complete(prompt, system=None, role=None):
        raise RuntimeError("backend down")

    res = task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                   complete=complete, verify=lambda out, crit: (True, ""),
                   max_retries=2)
    assert res["escalated"] is True
    assert res["ok"] is False


def test_no_verify_hook_accepts_first_output():
    cfg = Config()
    res = task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                   complete=lambda *a, **k: _envelope("x"))
    assert res["ok"] is True
    assert res["retries"] == 0


def test_non_envelope_output_wrapped():
    cfg = Config()
    res = task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                   complete=lambda *a, **k: "plain text not json")
    assert res["output"] == "plain text not json"
    assert res["self_confidence"] is None
