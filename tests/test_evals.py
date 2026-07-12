# SPDX-License-Identifier: Apache-2.0
"""The eval harness: deterministic checks, scoring, offline injected runs, and
the wiring into eval-gated model promotion. No network, no live model."""

from __future__ import annotations

from cheapskate.evals import (
    DEFAULT_EVAL_SET,
    EvalTask,
    decision_from_evals,
    eval_set_for_role,
    make_eval_fn,
    roles_covered,
    run_eval_set,
    summarize,
)
from cheapskate.evals.fixtures import (
    contains_all,
    contains_none,
    equals_normalized,
    is_json_with_keys,
)
from cheapskate.evals.runner import EvalResult, canned_complete, perfect_complete


# ── the shipped set is well-formed ───────────────────────────────────────────


def test_default_eval_set_spans_the_shipped_roles():
    roles = roles_covered()
    assert set(roles) == {"reasoning", "classification", "code"}
    # ~6-10 tasks, some critical
    assert 6 <= len(DEFAULT_EVAL_SET) <= 10
    assert any(t.critical for t in DEFAULT_EVAL_SET)
    # every task carries a callable deterministic check
    assert all(callable(t.check) for t in DEFAULT_EVAL_SET)


def test_eval_set_for_role_filters():
    reasoning = eval_set_for_role("reasoning")
    assert reasoning and all(t.role == "reasoning" for t in reasoning)
    assert eval_set_for_role("nonesuch") == []


# ── deterministic check builders ─────────────────────────────────────────────


def test_contains_all_case_insensitive():
    ok, _ = contains_all("Moon")("...landed on the moon in 1969")
    assert ok
    bad, detail = contains_all("mars")("...landed on the moon")
    assert not bad and "missing" in detail


def test_contains_none_negative_constraint():
    ok, _ = contains_none("sunday")("open 9 to 5 on weekdays")
    assert ok
    bad, _ = contains_none("sunday")("also open sunday")
    assert not bad


def test_equals_normalized_tolerates_wrapping():
    check = equals_normalized("positive")
    assert check("positive")[0]
    assert check("Positive.")[0]
    assert check("The sentiment is positive")[0]
    assert not check("negative")[0]


def test_is_json_with_keys_scans_fenced_and_prose():
    check = is_json_with_keys("name", "age")
    assert check('{"name": "Dana", "age": 34}')[0]
    assert check('Here you go:\n```json\n{"name":"Dana","age":34}\n```')[0]
    assert not check('{"name": "Dana"}')[0]  # missing age
    assert not check("no json here")[0]


# ── scoring ──────────────────────────────────────────────────────────────────


def test_summarize_scores_pass_rate_and_criticals():
    results = [
        EvalResult("a", "reasoning", "summarize", True, True, "ok"),
        EvalResult("b", "reasoning", "extract", True, False, "bad"),
        EvalResult("c", "classification", "classify", False, True, "ok"),
    ]
    s = summarize(results)
    assert s["total"] == 3
    assert s["passed"] == 2
    assert s["critical_total"] == 2
    assert s["critical_passed"] == 1
    assert abs(s["pass_rate"] - 2 / 3) < 1e-9


def test_summarize_empty_set_fails_closed():
    s = summarize([])
    assert s["pass_rate"] == 0.0
    assert s["total"] == 0
    # critical_pass_rate defaults to 1.0 (no critical tasks) but a real gate keys
    # off pass_rate, which is 0 — an empty set never passes.


# ── offline injected runs ────────────────────────────────────────────────────


def test_perfect_complete_scores_full_green():
    s = run_eval_set(perfect_complete())
    assert s["pass_rate"] == 1.0
    assert s["critical_passed"] == s["critical_total"] == 4


def test_a_wrong_model_fails_criticals():
    # returns the empty string for everything → every check that needs a substring
    # or JSON fails.
    s = run_eval_set(canned_complete({}, default="I don't know."))
    assert s["critical_passed"] < s["critical_total"]
    assert s["pass_rate"] < 1.0


def test_a_raising_model_is_scored_as_failures_not_a_crash():
    def boom(prompt, *, system=None, role=None, model=None):
        raise RuntimeError("backend down")

    s = run_eval_set(boom)
    assert s["passed"] == 0
    assert all(r["error"] == "RuntimeError" for r in s["results"])


def test_run_eval_set_filters_by_role():
    s = run_eval_set(perfect_complete(), role="code")
    assert s["total"] == len(eval_set_for_role("code"))
    assert all(r["role"] == "code" for r in s["results"])


# ── eval-gated promotion wiring ──────────────────────────────────────────────


def test_make_eval_fn_shapes_currency_summary():
    eval_fn = make_eval_fn(perfect_complete())
    summary = eval_fn("some-model", "ollama")
    # exactly the fields registry.currency.evaluate/decision consume
    assert "pass_rate" in summary
    assert "critical_passed" in summary
    assert "critical_total" in summary
    assert summary["backend"] == "ollama"


def test_decision_promotes_when_candidate_clears_floor_and_margin():
    decide = decision_from_evals(critical_floor=1.0, margin=0.0)
    inc = {"pass_rate": 0.75, "critical_pass_rate": 1.0}
    cand = {"pass_rate": 0.90, "critical_pass_rate": 1.0}
    d = decide(inc, cand, False)
    assert d["promote"] is True


def test_decision_refuses_candidate_that_drops_a_critical():
    decide = decision_from_evals(critical_floor=1.0)
    inc = {"pass_rate": 0.5, "critical_pass_rate": 1.0}
    cand = {"pass_rate": 0.99, "critical_pass_rate": 0.75}  # dropped a critical
    d = decide(inc, cand, True)
    assert d["promote"] is False
    assert "critical" in d["reason"]


def test_decision_same_lineage_tie_promotes_but_cross_lineage_needs_margin():
    decide = decision_from_evals(critical_floor=1.0, margin=0.05)
    inc = {"pass_rate": 0.80, "critical_pass_rate": 1.0}
    tie = {"pass_rate": 0.80, "critical_pass_rate": 1.0}
    assert decide(inc, tie, True)["promote"] is True  # same lineage: tie ok
    assert decide(inc, tie, False)["promote"] is False  # cross lineage: needs +margin


def test_custom_eval_set_flows_through():
    custom = [
        EvalTask(
            id="t1", role="reasoning", task_type="summarize",
            prompt="say hello", check=contains_all("hello"), critical=True,
        )
    ]
    s = run_eval_set(canned_complete({"say hello": "hello there"}), eval_set=custom)
    assert s["total"] == 1 and s["passed"] == 1
