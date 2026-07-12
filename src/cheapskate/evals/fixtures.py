# SPDX-License-Identifier: Apache-2.0
"""The shipped default eval set: small, deterministic fixture tasks.

Each :class:`EvalTask` pairs a prompt with a DETERMINISTIC check — a substring
match, an exact-match, or a JSON-shape assertion — so a pass is reproducible on
any machine, offline, with no grader model. This is a quality GATE, not a
benchmark: ~8 tasks spanning the roles a shipped install actually routes to
(``reasoning`` for summarize/extract/draft, ``classification`` for classify,
``code`` for review). ``critical`` tasks must pass for a candidate to be
promotable; non-critical tasks contribute to the pass-rate margin.

The checks are intentionally forgiving of formatting (case, surrounding prose)
and strict on substance, because a real local model's phrasing varies but the
FACT it must produce does not. Nothing here reads the network or the filesystem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


# A check: (model_output_text) -> (passed, detail). Pure, deterministic.
CheckFn = Callable[[str], "tuple[bool, str]"]


@dataclass(frozen=True)
class EvalTask:
    """One deterministic fixture task.

    ``role`` names the router role this task exercises (so the harness can run
    only the tasks relevant to the role being evaluated). ``task_type`` is the
    human-facing category (summarize/classify/extract/review). ``critical`` tasks
    gate promotion; non-critical ones set the margin.
    """

    id: str
    role: str
    task_type: str
    prompt: str
    check: CheckFn
    critical: bool = False
    system: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


# ── deterministic check builders ─────────────────────────────────────────────


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def contains_all(*needles: str) -> CheckFn:
    """Pass iff every needle appears (case-insensitively) in the output."""

    def _check(out: str) -> tuple[bool, str]:
        n = _norm(out)
        missing = [w for w in needles if _norm(w) not in n]
        if missing:
            return (False, f"missing required substrings: {missing}")
        return (True, "all required substrings present")

    return _check


def contains_none(*needles: str) -> CheckFn:
    """Pass iff NONE of the needles appear — a negative constraint (e.g. the
    model must not leak a forbidden token)."""

    def _check(out: str) -> tuple[bool, str]:
        n = _norm(out)
        present = [w for w in needles if _norm(w) in n]
        if present:
            return (False, f"forbidden substrings present: {present}")
        return (True, "no forbidden substrings")

    return _check


def equals_normalized(expected: str) -> CheckFn:
    """Pass iff the output, whitespace/case-normalized and stripped of trailing
    punctuation, equals ``expected`` normalized. For one-word classifications."""
    want = _norm(expected).strip(".!?\"' ")

    def _check(out: str) -> tuple[bool, str]:
        got = _norm(out).strip(".!?\"' ")
        # tolerate the model wrapping the label in a short sentence
        ok = got == want or got.endswith(want) or (want in got.split())
        return (ok, "exact/label match" if ok else f"expected {want!r}, got {got!r}")

    return _check


def is_json_with_keys(*required_keys: str) -> CheckFn:
    """Pass iff the output parses as a JSON object containing every required key.

    Tolerates the output being wrapped in prose or a ```json code fence — it
    scans for the first balanced ``{...}`` object and parses that.
    """

    def _check(out: str) -> tuple[bool, str]:
        obj = _extract_json_object(out)
        if obj is None:
            return (False, "no JSON object found in output")
        if not isinstance(obj, dict):
            return (False, f"JSON is not an object (got {type(obj).__name__})")
        missing = [k for k in required_keys if k not in obj]
        if missing:
            return (False, f"JSON missing keys: {missing}")
        return (True, f"JSON object with keys {list(required_keys)}")

    return _check


def _extract_json_object(text: str) -> Any:
    """Best-effort extraction of the first top-level JSON object from ``text``."""
    if not text:
        return None
    s = text.strip()
    # Fast path: the whole thing is JSON.
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        pass
    # Scan for the first balanced {...}.
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    chunk = s[start : i + 1]
                    try:
                        return json.loads(chunk)
                    except (json.JSONDecodeError, TypeError):
                        break
        start = s.find("{", start + 1)
    return None


# ── the shipped eval set ─────────────────────────────────────────────────────

# ~8 fixture tasks. ``critical`` marks the floor a candidate MUST clear.
DEFAULT_EVAL_SET: list[EvalTask] = [
    # summarize (role: reasoning) ------------------------------------------------
    EvalTask(
        id="summarize_key_fact",
        role="reasoning",
        task_type="summarize",
        prompt=(
            "Summarize in one sentence: The Apollo 11 mission landed the first "
            "humans on the Moon on July 20, 1969. Neil Armstrong and Buzz Aldrin "
            "walked on the surface while Michael Collins orbited above."
        ),
        check=contains_all("moon"),
        critical=True,
    ),
    EvalTask(
        id="summarize_no_hallucination",
        role="reasoning",
        task_type="summarize",
        prompt=(
            "Summarize this text in one sentence, using ONLY facts stated: "
            "The library opens at 9am and closes at 5pm on weekdays."
        ),
        # It must not invent a weekend claim that was never stated.
        check=contains_none("sunday", "24 hours", "midnight"),
        critical=False,
    ),
    # extract (role: reasoning) --------------------------------------------------
    EvalTask(
        id="extract_json_fields",
        role="reasoning",
        task_type="extract",
        prompt=(
            "Extract the name and age as a JSON object with keys 'name' and "
            "'age' from: 'Dana Reyes is 34 years old and lives in Denver.' "
            "Return ONLY the JSON object."
        ),
        check=is_json_with_keys("name", "age"),
        critical=True,
    ),
    EvalTask(
        id="extract_value_present",
        role="reasoning",
        task_type="extract",
        prompt=(
            "From the sentence 'The invoice total is $128.50 due on March 3.', "
            "extract the total amount. Include the number in your answer."
        ),
        check=contains_all("128.50"),
        critical=False,
    ),
    # classify (role: classification) -------------------------------------------
    EvalTask(
        id="classify_sentiment_positive",
        role="classification",
        task_type="classify",
        prompt=(
            "Classify the sentiment of this review as exactly one word — "
            "positive, negative, or neutral: 'This is the best coffee I have "
            "ever had, absolutely fantastic!' Answer with only the label."
        ),
        check=equals_normalized("positive"),
        critical=True,
    ),
    EvalTask(
        id="classify_sentiment_negative",
        role="classification",
        task_type="classify",
        prompt=(
            "Classify the sentiment as exactly one word — positive, negative, or "
            "neutral: 'The product broke after one day and support ignored me.' "
            "Answer with only the label."
        ),
        check=equals_normalized("negative"),
        critical=False,
    ),
    # review (role: code) --------------------------------------------------------
    EvalTask(
        id="review_spot_bug",
        role="code",
        task_type="review",
        prompt=(
            "Review this Python function for a bug and name the problem:\n"
            "def average(nums):\n    return sum(nums) / len(nums)\n"
            "What happens if nums is empty?"
        ),
        # Any correct review names the empty-list / zero-division failure.
        check=contains_all("zero"),
        critical=True,
    ),
    EvalTask(
        id="review_json_verdict",
        role="code",
        task_type="review",
        prompt=(
            "Review the change 'renamed variable x to total for clarity' and "
            "return a JSON object with keys 'verdict' and 'reason'. "
            "verdict must be 'approve' or 'request-changes'."
        ),
        check=is_json_with_keys("verdict", "reason"),
        critical=False,
    ),
]


def roles_covered(eval_set: list[EvalTask] | None = None) -> list[str]:
    """The distinct roles the eval set exercises."""
    tasks = eval_set if eval_set is not None else DEFAULT_EVAL_SET
    return sorted({t.role for t in tasks})


def eval_set_for_role(role: str, eval_set: list[EvalTask] | None = None) -> list[EvalTask]:
    """The subset of tasks that exercise ``role``. An unknown role yields []."""
    tasks = eval_set if eval_set is not None else DEFAULT_EVAL_SET
    return [t for t in tasks if t.role == role]
