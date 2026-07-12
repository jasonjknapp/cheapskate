# SPDX-License-Identifier: Apache-2.0
"""The verify-and-repair primitive: run one supervised subtask.

Flow:
  1. Decide the route (``routes.route_decision``) at the current dial.
     never_local / never_cloud fail closed here — a refusal is raised, never
     silently downgraded.
  2. For a local (or forced-local) route, delegate to the model, parse the
     worker's JSON envelope, and hand the output to a caller-supplied ``verify``
     hook.
  3. On a failed verification, repair: re-delegate with the verifier's feedback,
     bounded to ``max_retries`` (default 2). After the budget is spent the result
     is flagged ``escalated=True`` — the SIGNAL that the caller should escalate
     to a stronger tier. This module never silently falls back to the cloud.

The model call is injected (``complete=``) so tests never touch a live server;
by default it binds to ``cheapskate.client.complete`` lazily inside the function.
Telemetry is content-free (counts, durations, retries, escalation — no text).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .. import telemetry
from ..config import Config
from . import routes
from .dial import read_dial

# The worker is asked to answer inside a small JSON envelope so we get a
# self-reported confidence + criteria flag alongside the output.
ENVELOPE_SYSTEM = (
    "You are a worker model in a supervised pipeline. Satisfy the ACCEPTANCE "
    "CRITERIA exactly. Return ONLY a JSON object: "
    '{"output": <your answer as a string, or the requested JSON value>, '
    '"self_confidence": <number 0..1, your honest estimate you met every criterion>, '
    '"criteria_met": <true|false>}. No prose outside the JSON.'
)

# The verify hook: (output, criteria) -> (ok, feedback). ok True accepts; False
# repairs with the feedback string appended to the next attempt.
VerifyFn = Callable[[str, str], tuple[bool, str]]
# The model call: (prompt, system, role) -> raw string.
CompleteFn = Callable[..., str]


class NeverLocal(routes.NeverLocal):
    """Re-exported so callers can ``except task.NeverLocal``."""


class NeverCloud(routes.NeverCloud):
    """Re-exported so callers can ``except task.NeverCloud``."""


def _build_prompt(criteria: str, payload: str, feedback: str | None = None) -> str:
    parts = [f"ACCEPTANCE CRITERIA:\n{criteria}", f"INPUT:\n{payload}"]
    if feedback:
        parts.append(f"A prior attempt was rejected. Fix this:\n{feedback}")
    parts.append("Produce the output that satisfies every criterion.")
    return "\n\n".join(parts)


def _parse_envelope(content: str) -> dict[str, Any]:
    """Best-effort parse of the worker's JSON envelope; wrap raw text if it
    isn't valid JSON with an ``output`` key."""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "output" in obj:
            return {
                "output": obj["output"],
                "self_confidence": obj.get("self_confidence"),
                "criteria_met": obj.get("criteria_met"),
            }
    except (json.JSONDecodeError, TypeError):
        pass
    return {"output": content, "self_confidence": None, "criteria_met": None}


def _default_complete() -> CompleteFn:
    # Bound lazily so importing this module never pulls in the client (and its
    # broker dependency); tests inject ``complete=`` and never hit this.
    # client.complete returns a rich dict ({text, model, latency_s, ...});
    # the task contract is text-in/text-out, so adapt here.
    from .. import client

    def _text_complete(prompt: str, **kwargs: Any) -> str:
        return client.complete(prompt, **kwargs)["text"]

    return _text_complete


def _as_text(output: Any) -> str:
    return output if isinstance(output, str) else json.dumps(output)


def run(
    task_type: str,
    criteria: str,
    payload: str,
    config: Config,
    *,
    verify: VerifyFn | None = None,
    complete: CompleteFn | None = None,
    dial: tuple[int, str | None] | None = None,
    max_retries: int = 2,
) -> dict[str, Any]:
    """Route + run one supervised subtask. Returns a result dict.

    Raises :class:`NeverLocal` / :class:`NeverCloud` for a fail-closed refusal.
    A cloud / cloud-downgraded / unknown route returns a descriptor without
    calling a model (the caller handles those tiers). A local route delegates,
    verifies, and repairs up to ``max_retries`` before flagging ``escalated``.
    """
    dial = dial if dial is not None else read_dial(config)
    decision = routes.route_decision(task_type, dial, config)
    route = decision["route"]

    if route == routes.REFUSE_NEVER_LOCAL:
        telemetry.log_event(
            "task.route", task_type=task_type, route="refused", ok=False,
            error_kind="never_local",
        )
        raise NeverLocal(decision["reason"])
    if route == routes.REFUSE_NEVER_CLOUD:
        telemetry.log_event(
            "task.route", task_type=task_type, route="refused", ok=False,
            error_kind="never_cloud",
        )
        raise NeverCloud(decision["reason"])

    if route in (routes.CLOUD, routes.CLOUD_DOWNGRADED, routes.UNKNOWN):
        telemetry.log_event(
            "task.route", task_type=task_type,
            route="cloud" if route != routes.UNKNOWN else "refused", ok=True,
        )
        return {
            **decision,
            "output": None,
            "model": None,
            "note": "handled by the caller (not routed local)",
        }

    # route == LOCAL
    complete = complete or _default_complete()
    role = decision.get("role", "reasoning")
    patience = decision.get("escalation_patience", "escalate-fast")

    started = time.monotonic()
    # We make up to (max_retries + 1) model calls: the first attempt plus at most
    # max_retries repairs. ``retries`` is the number of repair attempts issued
    # after the first (attempts - 1), so a run that exhausts the budget reports
    # retries == max_retries, not max_retries + 1.
    attempts = 0
    max_attempts = max_retries + 1
    feedback: str | None = None
    last_env: dict[str, Any] = {"output": None, "self_confidence": None, "criteria_met": None}
    ok = False
    error_kind: str | None = None

    while attempts < max_attempts:
        attempts += 1
        prompt = _build_prompt(criteria, payload, feedback)
        try:
            raw = complete(prompt, system=ENVELOPE_SYSTEM, role=role)
        except Exception as exc:  # noqa: BLE001 — a model/backend failure is a repairable attempt
            error_kind = type(exc).__name__
            feedback = f"model call failed: {error_kind}"
            continue
        last_env = _parse_envelope(raw)
        if verify is None:
            ok = True
            break
        accepted, fb = verify(_as_text(last_env["output"]), criteria)
        if accepted:
            ok = True
            break
        error_kind = "verify_failed"
        feedback = fb

    retries = attempts - 1
    escalated = not ok
    duration_s = round(time.monotonic() - started, 3)
    telemetry.log_event(
        "task.run",
        task_type=task_type,
        route="local",
        model=role,
        retries=retries,
        escalated=escalated,
        ok=ok,
        duration_s=duration_s,
        error_kind=error_kind if not ok else None,
    )
    return {
        "task_type": task_type,
        "route": "local",
        "role": role,
        "output": last_env["output"],
        "self_confidence": last_env.get("self_confidence"),
        "criteria_met": last_env.get("criteria_met"),
        "ok": ok,
        "retries": retries,
        "escalated": escalated,
        "escalation_patience": patience,
        "duration_s": duration_s,
        "reminder": "caller verifies output vs criteria; escalated=True means escalate to a stronger tier",
    }
