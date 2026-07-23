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

    def complete(prompt, system=None, role=None, model=None):
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

    def complete(prompt, system=None, role=None, model=None):
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


def test_local_job_repairs_then_switches_to_role_fallback():
    cfg = Config(roles={"reasoning": {
        "model": "org/incumbent",
        "backend": "mlx",
        "fallback": "fallback:latest",
    }})
    calls = []

    def complete(prompt, system=None, role=None, model=None):
        calls.append((role, model))
        if model == "org/incumbent":
            return _envelope("bad")
        return _envelope("good")

    res = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=complete,
        verify=lambda out, crit: (out == "good", "quality floor"),
        max_retries=1,
    )
    assert res["ok"] is True
    assert res["model"] == "fallback:latest"
    assert calls == [
        (None, "org/incumbent"),
        (None, "org/incumbent"),
        (None, "fallback:latest"),
    ]


def test_missing_role_normalizes_to_router_local_unavailable(monkeypatch):
    """When role resolution raises the resolver's internal LocalUnavailable, the
    router surfaces its OWN LocalUnavailable — the class callers are documented to
    catch — not the resolver's identically-named internal exception."""
    import sys

    import cheapskate.backends.resolve  # noqa: F401 — ensure the submodule is loaded
    _resolve = sys.modules["cheapskate.backends.resolve"]

    def boom(*_a, **_k):
        raise _resolve.LocalUnavailable("role 'ghost' has no model configured")

    monkeypatch.setattr(_resolve, "role_candidates", boom)
    cfg = Config()
    with pytest.raises(task.LocalUnavailable):
        task.run("summarize", "crit", "data", cfg, dial=(2, "std"),
                 complete=lambda *a, **k: _envelope("x"))


def test_exhausted_local_run_attributes_to_last_model_tried():
    """When every candidate exhausts, the run's error/model must name the last
    model actually attempted (the fallback), not the incumbent it started on."""
    cfg = Config(roles={"reasoning": {
        "model": "org/incumbent",
        "backend": "mlx",
        "fallback": "fallback:latest",
    }})

    res = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=lambda *a, **k: _envelope("bad"),
        verify=lambda out, crit: (False, "never good"),
        max_retries=0,
    )
    assert res["ok"] is False
    assert res["model"] == "fallback:latest"


def test_mixed_quality_then_transport_failure_does_not_cross_attribute_output():
    """Incumbent produces output that fails verify, then the fallback fails to
    return content: the exhausted result must NOT claim the fallback model
    produced the incumbent's output (last_env must reset per candidate)."""
    cfg = Config(roles={"reasoning": {
        "model": "org/incumbent",
        "backend": "mlx",
        "fallback": "fallback:latest",
    }})

    def complete(prompt, system=None, role=None, model=None):
        if model == "org/incumbent":
            return _envelope("incumbent-said-this")
        raise RuntimeError("fallback transport down")

    res = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=complete,
        verify=lambda out, crit: (False, "reject"),
        max_retries=0,
    )
    assert res["ok"] is False
    assert res["model"] == "fallback:latest"
    assert res["output"] != "incumbent-said-this"  # no cross-model attribution
    assert res["output"] is None


def test_local_job_rejects_broker_model_identity_mismatch():
    cfg = Config(roles={"reasoning": {
        "model": "org/incumbent", "backend": "mlx",
    }})

    result = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=lambda *a, **k: {
            "text": _envelope("looks valid"), "model": "hidden-fallback",
        },
        verify=lambda out, crit: (True, ""), max_retries=0,
    )
    assert result["ok"] is False
    assert result["model"] == "org/incumbent"
    assert result["error_kind"] == "LocalUnavailable"


def test_exhausted_local_run_emits_exactly_one_escalated_generation(monkeypatch):
    # R1: escalations must reach the receipts. A local run that exhausts its
    # retries (verify always rejects) emits one kind="generation" event PER
    # ATTEMPT, and EXACTLY ONE of them — the terminal attempt — carries
    # escalated=True. Earlier attempts stay escalated=False.
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        task.telemetry, "log_event",
        lambda kind, **fields: captured.append((kind, fields)),
    )
    cfg = Config()
    res = task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=lambda *a, **k: _envelope("nope"),
        verify=lambda out, crit: (False, "still bad"),
        max_retries=2,
    )
    assert res["escalated"] is True and res["ok"] is False
    gens = [f for kind, f in captured if kind == "generation"]
    assert len(gens) == 3  # 1 initial + 2 retries, one event each
    escalated = [g for g in gens if g["escalated"]]
    assert len(escalated) == 1  # exactly the terminal attempt
    assert escalated[0]["route"] == "local"
    assert escalated[0]["ok"] is False


def test_successful_local_run_never_emits_escalated(monkeypatch):
    # A run that succeeds on any attempt breaks the loop → every emitted
    # generation event has escalated=False.
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        task.telemetry, "log_event",
        lambda kind, **fields: captured.append((kind, fields)),
    )
    cfg = Config()
    task.run(
        "summarize", "crit", "data", cfg, dial=(2, "std"),
        complete=lambda *a, **k: _envelope("done"),
        verify=lambda out, crit: (True, ""),
    )
    gens = [f for kind, f in captured if kind == "generation"]
    assert gens and all(g["escalated"] is False for g in gens)


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
