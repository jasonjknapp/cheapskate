# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the local end-to-end task path fixes (D1-D7).

Each test pins one defect found when the "point a role at a small local model and
run one task" path was first exercised end to end. All are hermetic: no live
broker, no network, no real model. See docs/ARCHITECTURE.md for the path.
"""

from __future__ import annotations

import json

import pytest

from cheapskate.backends.resolve import LocalUnavailable, resolve
from cheapskate.config import Config
from cheapskate.router import task


@pytest.fixture(autouse=True)
def _quiet_telemetry(monkeypatch):
    events = []
    monkeypatch.setattr(task.telemetry, "log_event", lambda *a, **k: events.append((a, k)))
    return events


# ── D1: role: prefix decode ───────────────────────────────────────────────────


def test_d1_resolve_decodes_role_prefix_from_config():
    cfg = {"roles": {"code": {"model": "qwen3:4b", "backend": "ollama"}}}
    spec = resolve(model="role:code", config=cfg)
    assert spec.model == "qwen3:4b"
    assert spec.backend == "ollama"
    assert spec.role == "code"


def test_d1_resolve_decodes_role_prefix_from_shipped_defaults():
    # No config, no registry: the shipped default fleet must still resolve.
    spec = resolve(model="role:code")
    assert spec.role == "code"
    assert not spec.model.startswith("role:")


def test_d1_resolve_unknown_role_prefix_raises():
    with pytest.raises(LocalUnavailable):
        resolve(model="role:does-not-exist", config={"roles": {}})


def test_d1_role_pointing_at_another_role_is_rejected():
    # A misconfigured role whose model is itself a role: pointer must fail with a
    # clear error, not resolve to a literal that 404s downstream.
    with pytest.raises(LocalUnavailable, match="not a concrete model"):
        resolve(model="role:a", config={"roles": {"a": {"model": "role:b"}}})


def test_d1_plain_model_id_unaffected():
    spec = resolve(model="qwen3:4b", config={"roles": {}})
    assert spec.model == "qwen3:4b"
    assert spec.role is None


# ── D3: a failed local run carries error_kind (so the CLI can surface it) ──────


def test_d3_failed_local_run_reports_error_kind():
    cfg = Config()

    def boom(*a, **k):
        raise ConnectionError("broker unreachable")

    res = task.run("summarize", "crit", "data", cfg, dial=(3, None), complete=boom)
    assert res["ok"] is False
    assert res["output"] is None
    assert res["error_kind"] == "ConnectionError"
    assert res["escalated"] is True


# ── D4: envelope parsing tolerates real small-model output ─────────────────────


@pytest.mark.parametrize(
    "raw,expected_output,expected_met",
    [
        ("Paris is the capital.", "Paris is the capital.", None),  # plain text
        ('```json\n{"output": "Paris", "criteria_met": true}\n```', "Paris", True),  # fenced
        ('<think>hmm</think>\n{"output": "Paris", "self_confidence": 0.9}', "Paris", None),  # reasoning
        ('Here you go: {"output": "Paris", "criteria_met": true} thanks', "Paris", True),  # prose-wrapped
        ("   ", None, None),  # empty
    ],
)
def test_d4_parse_envelope_variants(raw, expected_output, expected_met):
    env = task._parse_envelope(raw)
    assert env["output"] == expected_output
    assert env["criteria_met"] == expected_met


def test_d4_plain_text_answer_accepted_when_no_verify():
    """With no verify function (the CLI default), a plain-text answer that is not
    JSON-wrapped is accepted as the output rather than escalating."""
    cfg = Config()
    res = task.run(
        "summarize", "crit", "data", cfg, dial=(3, None),
        complete=lambda *a, **k: "The answer in plain prose.",
    )
    assert res["ok"] is True
    assert res["output"] == "The answer in plain prose."


# ── D5: token counts from a rich completion reach the generation telemetry ─────


def test_d5_local_run_records_token_counts(_quiet_telemetry):
    cfg = Config()
    rich = {"text": json.dumps({"output": "done", "criteria_met": True}),
            "prompt_eval_count": 42, "eval_count": 17}
    task.run(
        "summarize", "crit", "data", cfg, dial=(3, None),
        complete=lambda *a, **k: rich,
    )
    # find the generation event that carried the token counts
    tokened = [k for (a, k) in _quiet_telemetry if k.get("tokens_in") == 42]
    assert tokened, "no generation event carried the prompt token count"
    assert tokened[0]["tokens_out"] == 17


def test_d5_bare_string_completion_records_no_tokens(_quiet_telemetry):
    cfg = Config()
    res = task.run(
        "summarize", "crit", "data", cfg, dial=(3, None),
        complete=lambda *a, **k: "plain answer",
    )
    assert res["ok"] is True and res["output"] == "plain answer"
    # A bare-string completion carries no token counts, so no generation event
    # may report a non-None tokens_in/tokens_out.
    for (a, k) in _quiet_telemetry:
        if a and a[0] == "generation":
            assert k.get("tokens_in") is None
            assert k.get("tokens_out") is None


# ── completion normalizers ─────────────────────────────────────────────────────


def test_completion_normalizers():
    assert task._completion_text({"text": "hi"}) == "hi"
    assert task._completion_text("hi") == "hi"
    assert task._completion_tokens({"prompt_eval_count": 3, "eval_count": 5}) == (3, 5)
    assert task._completion_tokens("hi") == (None, None)
    assert task._completion_tokens({"text": "hi"}) == (None, None)
