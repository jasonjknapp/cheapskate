# SPDX-License-Identifier: Apache-2.0
"""Run the eval set and score it — deterministically, offline by default.

The runner is agnostic to HOW a model is called: it takes a ``complete``
callable ``(prompt, *, system=None, role=None, model=None) -> str`` and runs each
fixture task through it, applying that task's deterministic check. In tests (and
in CI's ``--injected`` mode) ``complete`` is a canned callable — no network, no
live server, per the repo's test rules. A ``--live`` CLI run binds ``complete``
to :func:`cheapskate.client.complete` so the same set can gate a real model.

The scored summary is shaped EXACTLY like the ``EvalFn`` result the currency
engine consumes (``pass_rate``, ``critical_passed``, ``critical_total``), so
:func:`make_eval_fn` + :func:`decision_from_evals` wire this harness straight
into eval-gated promotion with no bespoke glue.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .fixtures import DEFAULT_EVAL_SET, EvalTask, eval_set_for_role


# The model call the harness drives. Kept minimal on purpose so an injected fake,
# ``cheapskate.client.complete``, or a broker call all satisfy it.
CompleteFn = Callable[..., str]


@dataclass
class EvalResult:
    """The outcome of one fixture task."""

    task_id: str
    role: str
    task_type: str
    critical: bool
    passed: bool
    detail: str
    error: str | None = None


def run_eval_set(
    complete: CompleteFn,
    *,
    role: str | None = None,
    model: str | None = None,
    eval_set: list[EvalTask] | None = None,
) -> dict[str, Any]:
    """Run the eval set (optionally filtered to ``role``) through ``complete``.

    ``complete`` is called ``complete(prompt, system=..., role=..., model=...)``
    and must return the model's text. A call that raises is scored as a FAILED
    task (never crashes the run) — a model that errors fails the eval, exactly
    like a wrong answer. Returns the scored summary from :func:`summarize`.
    """
    tasks = eval_set if eval_set is not None else DEFAULT_EVAL_SET
    if role is not None:
        tasks = eval_set_for_role(role, tasks)

    results: list[EvalResult] = []
    for t in tasks:
        try:
            out = complete(t.prompt, system=t.system, role=t.role, model=model)
        except Exception as exc:  # noqa: BLE001 - a model/backend error fails the task
            results.append(
                EvalResult(
                    task_id=t.id, role=t.role, task_type=t.task_type,
                    critical=t.critical, passed=False,
                    detail="model call raised", error=type(exc).__name__,
                )
            )
            continue
        passed, detail = t.check(out if isinstance(out, str) else str(out))
        results.append(
            EvalResult(
                task_id=t.id, role=t.role, task_type=t.task_type,
                critical=t.critical, passed=passed, detail=detail,
            )
        )
    return summarize(results, role=role, model=model)


def summarize(
    results: list[EvalResult], *, role: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """Score a list of :class:`EvalResult` into a currency-engine-shaped summary.

    Returns ``{pass_rate, passed, total, critical_passed, critical_total,
    critical_pass_rate, role, model, results}``. When the set is empty every
    count is 0 and ``pass_rate`` is 0.0 (an empty set never passes a gate —
    fail-closed).
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    crit = [r for r in results if r.critical]
    crit_total = len(crit)
    crit_passed = sum(1 for r in crit if r.passed)
    return {
        "pass_rate": (passed / total) if total else 0.0,
        "passed": passed,
        "total": total,
        "critical_passed": crit_passed,
        "critical_total": crit_total,
        "critical_pass_rate": (crit_passed / crit_total) if crit_total else 1.0,
        "role": role,
        "model": model,
        "results": [
            {
                "task_id": r.task_id, "task_type": r.task_type, "role": r.role,
                "critical": r.critical, "passed": r.passed,
                "detail": r.detail, "error": r.error,
            }
            for r in results
        ],
    }


# ── currency-engine wiring ───────────────────────────────────────────────────


def make_eval_fn(
    complete: CompleteFn, *, eval_set: list[EvalTask] | None = None
) -> Callable[[str, str], dict[str, Any]]:
    """Build the ``EvalFn`` that :func:`cheapskate.registry.currency.evaluate`
    calls: ``(model, backend) -> summary``. It runs the FULL eval set (every
    role) for the given model through ``complete``, pinning the concrete model so
    incumbent and candidate are scored on identical tasks. This is the hook that
    makes eval-gated promotion runnable from a bare clone.
    """

    def _eval_fn(model: str, backend: str) -> dict[str, Any]:
        # Score across every role the set covers; a promotion must not regress
        # any role. The model is pinned so the registry's role indirection is
        # bypassed and the CANDIDATE (not the incumbent) is what runs.
        summary = run_eval_set(complete, model=model, eval_set=eval_set)
        summary["backend"] = backend
        return summary

    return _eval_fn


def decision_from_evals(
    *, critical_floor: float = 1.0, margin: float = 0.0
) -> Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]]:
    """Build the ``DecisionFn`` for :func:`currency.evaluate`.

    Promote the candidate iff it clears the critical floor AND its overall
    pass_rate beats the incumbent by at least ``margin``. A same-lineage
    candidate (a point-release of the incumbent's family) is allowed to promote
    on a TIE (margin treated as 0) — a newer build of the same model that holds
    the line is worth taking; a different lineage must strictly beat the margin.
    Fail-closed: a candidate that drops any critical task is never promoted.
    """

    def _decide(
        inc: dict[str, Any], cand: dict[str, Any], same_lineage: bool
    ) -> dict[str, Any]:
        cand_crit = cand.get("critical_pass_rate", 0.0)
        if cand_crit < critical_floor:
            return {
                "promote": False,
                "reason": (
                    f"candidate critical pass-rate {cand_crit:.2f} < floor "
                    f"{critical_floor:.2f}"
                ),
            }
        inc_rate = inc.get("pass_rate", 0.0)
        cand_rate = cand.get("pass_rate", 0.0)
        needed = inc_rate + (0.0 if same_lineage else margin)
        if cand_rate < needed - 1e-9:
            return {
                "promote": False,
                "reason": (
                    f"candidate pass-rate {cand_rate:.2f} does not beat incumbent "
                    f"{inc_rate:.2f} by margin {margin:.2f}"
                    + (" (same-lineage tie allowed)" if same_lineage else "")
                ),
            }
        return {
            "promote": True,
            "reason": (
                f"candidate clears critical floor ({cand_crit:.2f}) and "
                f"pass-rate {cand_rate:.2f} >= required {needed:.2f}"
            ),
        }

    return _decide


def canned_complete(answers: dict[str, str], *, default: str = "") -> CompleteFn:
    """A deterministic offline ``complete`` for tests / injected CI runs: map a
    task id (or a prompt substring) to a canned answer. Any prompt that matches
    no key returns ``default``. Never touches a model or the network.

    Keys are matched against the task's prompt by substring, longest-key first,
    so a specific fixture can be targeted precisely.
    """

    def _complete(prompt: str, *, system: str | None = None, role: str | None = None,
                  model: str | None = None) -> str:
        for key in sorted(answers, key=len, reverse=True):
            if key in prompt:
                return answers[key]
        return default

    return _complete


def perfect_complete() -> CompleteFn:
    """A ``complete`` that returns a correct answer for every shipped fixture —
    used by injected-mode CI and the bare-clone proof to show the harness scores
    a green run WITHOUT any live model. Deterministic and offline.
    """
    canned = {
        "Apollo 11": "Apollo 11 landed the first humans on the Moon in 1969.",
        "The library opens at 9am": "The library is open 9am to 5pm on weekdays.",
        "Dana Reyes": '{"name": "Dana Reyes", "age": 34}',
        "The invoice total is $128.50": "The total amount is $128.50.",
        "best coffee": "positive",
        "The product broke after one day": "negative",
        "def average(nums)": "Bug: if nums is empty, len(nums) is 0 and this "
        "raises a ZeroDivisionError.",
        "renamed variable x to total": '{"verdict": "approve", "reason": '
        '"clearer name, no behavior change"}',
    }
    return canned_complete(canned)
