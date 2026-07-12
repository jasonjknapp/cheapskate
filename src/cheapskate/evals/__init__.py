# SPDX-License-Identifier: Apache-2.0
"""The eval harness: a small, shipped, deterministic quality gate.

This is NOT a benchmark suite. It is a handful of fixture tasks — the kind a
stranger cloning the repo can run in seconds — whose checks are deterministic
(exact-string / substring / JSON-shape), so ``pass`` means the same thing on
every machine. It exists to answer one question with a number: *does this model,
served this way, clear the bar for this role?* That number is what gates an
eval-gated model promotion in :mod:`cheapskate.registry.currency`.

Two public surfaces:

  * :func:`run_eval_set` — run the shipped (or a supplied) eval set for a role
    through an injectable ``complete`` callable, returning a summary dict shaped
    exactly like the ``EvalFn`` the currency engine expects
    (``{pass_rate, critical_passed, critical_total, ...}``).
  * :func:`make_eval_fn` / :func:`decision_from_evals` — adapters that wire the
    harness into ``registry.currency.evaluate`` so eval-gated promotion runs from
    a bare clone with no bespoke glue.
"""

from __future__ import annotations

from .fixtures import DEFAULT_EVAL_SET, EvalTask, eval_set_for_role, roles_covered
from .runner import (
    EvalResult,
    decision_from_evals,
    make_eval_fn,
    run_eval_set,
    summarize,
)

__all__ = [
    "DEFAULT_EVAL_SET",
    "EvalTask",
    "EvalResult",
    "eval_set_for_role",
    "roles_covered",
    "run_eval_set",
    "summarize",
    "make_eval_fn",
    "decision_from_evals",
]
