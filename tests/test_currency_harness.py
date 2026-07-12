# SPDX-License-Identifier: Apache-2.0
"""Eval-gated promotion, end-to-end through the shipped harness — the property
that makes model currency runnable from a bare clone. No network, no live model:
the harness's ``complete`` is an injected offline callable."""

from __future__ import annotations

from cheapskate.evals.runner import perfect_complete
from cheapskate.registry import currency


def _registry_with(role: str, incumbent: str, backend: str = "ollama") -> dict:
    return {"roles": {role: {"model": incumbent, "backend": backend}}}


def test_harness_promotes_a_candidate_that_holds_the_line():
    reg = _registry_with("reasoning", "old-model")
    plan = currency.evaluate_with_harness(
        "reasoning", "new-model", reg, complete=perfect_complete(),
    )
    # incumbent + candidate both score perfectly; a cross-lineage tie at the
    # default margin (0.0) promotes.
    assert plan["decision"]["promote"] is True
    assert plan["candidate"] == "new-model"
    assert plan["incumbent"] == "old-model"


def test_harness_refuses_a_candidate_that_drops_a_critical():
    reg = _registry_with("classification", "good-incumbent")

    # A candidate that returns garbage on the classify critical fails the floor.
    def complete(prompt, *, system=None, role=None, model=None):
        if model == "bad-candidate":
            return "banana"  # wrong label → classify critical fails
        return perfect_complete()(prompt, system=system, role=role, model=model)

    plan = currency.evaluate_with_harness(
        "classification", "bad-candidate", reg, complete=complete,
    )
    assert plan["decision"]["promote"] is False


def test_harness_respects_the_predownload_fit_gate():
    reg = _registry_with("reasoning", "incumbent")
    plan = currency.evaluate_with_harness(
        "reasoning", "too-big", reg,
        complete=perfect_complete(), fits=False, fit_reason="disk headroom",
    )
    # fails closed BEFORE running the eval suite
    assert plan["decision"]["promote"] is False
    assert "pre-download gate" in plan["decision"]["reason"]


def test_harness_respects_quarantine():
    reg = {
        "roles": {
            "reasoning": {
                "model": "incumbent", "backend": "ollama",
                "quarantine": ["known-bad"],
            }
        }
    }
    plan = currency.evaluate_with_harness(
        "reasoning", "known-bad", reg, complete=perfect_complete(),
    )
    assert plan["decision"]["promote"] is False
    assert "quarantin" in plan["decision"]["reason"].lower()


def test_harness_margin_blocks_a_cross_lineage_candidate_that_only_ties():
    reg = _registry_with("reasoning", "llama-old")
    # incumbent and candidate both perfect → tie; a cross-lineage candidate needs
    # a positive margin, so a tie is refused.
    plan = currency.evaluate_with_harness(
        "reasoning", "qwen-new", reg, complete=perfect_complete(), margin=0.10,
    )
    assert plan["decision"]["promote"] is False


def test_harness_evaluation_error_is_not_a_promotion(monkeypatch):
    reg = _registry_with("reasoning", "incumbent")

    def explode(prompt, *, system=None, role=None, model=None):
        raise ValueError("model exploded")

    # The harness scores a raising model as failing tasks (not an exception), so
    # the candidate simply fails the gate rather than crashing the engine.
    plan = currency.evaluate_with_harness(
        "reasoning", "candidate", reg, complete=explode,
    )
    assert plan["decision"]["promote"] is False
