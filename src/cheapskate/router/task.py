# SPDX-License-Identifier: Apache-2.0
"""The verify-and-repair primitive: run one supervised subtask.

Flow:
  1. Decide the route (``routes.route_decision``) at the current dial.
     never_local / never_cloud fail closed here — a refusal is raised, never
     silently downgraded.
  2. Wire the budget governor: before a cloud dispatch, ``econ.governor`` may
     tighten the dial for THIS request (over budget ⇒ run local instead). The
     tightening is applied per-request only — the dial state file is never
     written here.
  3. For a local (or forced-local) route, delegate to the local model; for a
     cloud route, dispatch through the cloud adapter for the task's role. Parse
     the worker's JSON envelope and hand the output to a caller-supplied
     ``verify`` hook.
  4. On a failed verification, repair: re-delegate with the verifier's feedback,
     bounded to ``max_retries`` (default 2). After the budget is spent the result
     is flagged ``escalated=True`` — the SIGNAL that the caller should escalate
     to a stronger tier.

Fail-closed, both directions, in the LIVE path:
  * never_local task → hard refusal, no local answer and no silent cloud fallback.
  * never_cloud task → never leaves the machine; if local is impossible it is a
    hard error, never shipped to the cloud.
  * cloud route with no enabled provider → hard :class:`CloudUnavailable` error
    with an actionable message.

The model call is injected (``complete=`` for local, ``cloud_dispatch=`` for
cloud) so tests never touch a live server; by default local binds to
``cheapskate.client.complete`` and cloud to ``cheapskate.cloud.dispatch_role``,
both lazily. Telemetry is content-free (counts, durations, retries, escalation,
tokens — no text): a dedicated ``kind="generation"`` event per attempt (the
costable unit the econ report/governor consume) plus a backward-compat
``kind="task.run"`` SUMMARY event that the report deliberately does NOT re-count.
"""

from __future__ import annotations

import json
import re
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
    "CRITERIA exactly. Prefer to return a single JSON object: "
    '{"output": <your answer as a string, or the requested JSON value>, '
    '"self_confidence": <number 0..1, your honest estimate you met every criterion>, '
    '"criteria_met": <true|false>}. '
    "If you cannot produce clean JSON, just return the answer as plain text: it "
    "will be accepted as the output. Do not wrap the JSON in markdown fences."
)

# The verify hook: (output, criteria) -> (ok, feedback). ok True accepts; False
# repairs with the feedback string appended to the next attempt.
VerifyFn = Callable[[str, str], tuple[bool, str]]
# The local model call: (prompt, system, role) -> raw string.
CompleteFn = Callable[..., str]
# The cloud dispatch: (config, role, prompt, system=) -> object with .text and
# .tokens_in / .tokens_out (a cheapskate.cloud.CloudResult by default).
CloudDispatchFn = Callable[..., Any]


class NeverLocal(routes.NeverLocal):
    """Re-exported so callers can ``except task.NeverLocal``."""


class NeverCloud(routes.NeverCloud):
    """Re-exported so callers can ``except task.NeverCloud``."""


class LocalUnavailable(Exception):
    """A never-cloud (or forced-local) task could not be served locally and there
    is NO cloud fallback — a hard, fail-closed error."""


class CloudUnavailable(Exception):
    """A cloud route could not be served: no enabled provider, no key, or a
    provider failure. A hard error — never silently downgraded to local."""


def _build_prompt(criteria: str, payload: str, feedback: str | None = None) -> str:
    parts = [f"ACCEPTANCE CRITERIA:\n{criteria}", f"INPUT:\n{payload}"]
    if feedback:
        parts.append(f"A prior attempt was rejected. Fix this:\n{feedback}")
    parts.append("Produce the output that satisfies every criterion.")
    return "\n\n".join(parts)


def _strip_reasoning(text: str) -> str:
    """Drop a leading ``<think>…</think>`` block that reasoning models emit
    before their real answer. Only a leading block is removed; if the tags are
    absent the text is returned unchanged."""
    m = re.match(r"\s*<think>.*?</think>\s*", text, flags=re.DOTALL | re.IGNORECASE)
    return text[m.end():] if m else text


def _extract_json_object(text: str) -> str | None:
    """The first balanced ``{…}`` object in ``text`` (spanning strings/escapes
    correctly), or None. Lets us find an envelope a model wrapped in prose or
    markdown fences without a brittle regex."""
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_envelope(content: str) -> dict[str, Any]:
    """Best-effort parse of the worker's JSON envelope. Tolerates a leading
    reasoning block, markdown ```` ```json ```` fences, and prose around the
    object. Falls back to treating the cleaned text as the plain-text output
    (with unknown confidence) when no ``{"output": …}`` envelope is present, so
    a small model that just answers in prose still works."""
    cleaned = _strip_reasoning(content or "")
    # Try the whole (cleaned) string, then the first balanced JSON object found
    # inside it (covers fenced ```json …``` and prose-wrapped envelopes).
    for candidate in (cleaned, _extract_json_object(cleaned)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict) and "output" in obj:
            return {
                "output": obj["output"],
                "self_confidence": obj.get("self_confidence"),
                "criteria_met": obj.get("criteria_met"),
            }
    return {"output": cleaned.strip() or None, "self_confidence": None, "criteria_met": None}


def _default_complete() -> CompleteFn:
    # Bound lazily so importing this module never pulls in the client (and its
    # broker dependency); tests inject ``complete=`` and never hit this.
    # client.complete returns a rich dict ({text, eval_count, prompt_eval_count,
    # ...}); we return the whole dict so _run_local can record token counts for
    # the econ receipt. _completion_text/_completion_tokens normalize both a
    # bare string (the historical contract, still honored for injected fns) and
    # this dict.
    from .. import client

    def _rich_complete(prompt: str, **kwargs: Any) -> dict[str, Any]:
        return client.complete(prompt, **kwargs)

    return _rich_complete


def _completion_text(raw: Any) -> str:
    """The answer text from a completion that may be a bare string (injected
    test fns, the historical contract) or the client's rich dict."""
    if isinstance(raw, dict):
        return raw.get("text") or ""
    return raw if isinstance(raw, str) else _as_text(raw)


def _completion_tokens(raw: Any) -> tuple[int | None, int | None]:
    """``(tokens_in, tokens_out)`` from a rich completion dict, or ``(None, None)``
    for a bare-string completion. Ollama reports ``prompt_eval_count`` /
    ``eval_count``; the client surfaces them under those keys."""
    if isinstance(raw, dict):
        ti = raw.get("prompt_eval_count")
        to = raw.get("eval_count")
        return (int(ti) if ti is not None else None,
                int(to) if to is not None else None)
    return (None, None)


def _default_cloud_dispatch() -> CloudDispatchFn:
    # Bound lazily so importing this module never pulls in the cloud SDKs
    # (optional extras); tests inject ``cloud_dispatch=`` and never hit this.
    from ..cloud import dispatch_role

    return dispatch_role


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
    cloud_dispatch: CloudDispatchFn | None = None,
    dial: tuple[int, str | None] | None = None,
    max_retries: int = 2,
    user: str = "interactive",
    govern: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Route + run one supervised subtask. Returns a result dict.

    Fail-closed, both directions:
      * never_local → :class:`NeverLocal` (no local answer, no silent fallback).
      * never_cloud that cannot stay local → :class:`NeverCloud` /
        :class:`LocalUnavailable` (never shipped off-box).
      * a cloud route with no enabled/mapping provider → :class:`CloudUnavailable`
        with an actionable message (never a silent local downgrade).

    Budget governor: before a cloud dispatch, the governor is consulted for
    ``user``; if their month-to-date spend crossed a threshold, its recommended
    dial is applied to THIS request only (the dial state file is never written)
    — an over-budget user's cloud-routable task runs local instead.

    Local routes delegate to ``complete``; cloud routes dispatch through
    ``cloud_dispatch``. Both verify + repair up to ``max_retries`` before
    flagging ``escalated``. Telemetry: one ``kind="generation"`` event per
    attempt plus a ``kind="task.run"`` summary (the latter not re-counted by the
    report)."""
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

    if route == routes.UNKNOWN:
        telemetry.log_event(
            "task.route", task_type=task_type, route="refused", ok=True,
        )
        return {
            **decision,
            "output": None,
            "model": None,
            "note": "handled by the caller (unknown task type)",
        }

    role = decision.get("role", "reasoning")
    patience = decision.get("escalation_patience", "escalate-fast")

    # A cloud (or cloud-downgraded) route: consult the budget governor first. If
    # the user is over budget the governor recommends a tighter dial; when that
    # forces local, re-decide the route locally instead of reaching the cloud.
    if route in (routes.CLOUD, routes.CLOUD_DOWNGRADED):
        governed = _apply_governor(config, user, dial, govern)
        if governed is not None and governed != dial:
            redecision = routes.route_decision(task_type, governed, config)
            if redecision["route"] == routes.LOCAL:
                # governor forced this cloud-routable task local for this request
                dial = governed
                decision = redecision
                route = routes.LOCAL
                role = decision.get("role", role)
                patience = decision.get("escalation_patience", patience)

    if route in (routes.CLOUD, routes.CLOUD_DOWNGRADED):
        cloud_dispatch = cloud_dispatch or _default_cloud_dispatch()
        return _run_cloud(
            task_type, criteria, payload, config, role, patience,
            verify=verify, cloud_dispatch=cloud_dispatch, max_retries=max_retries,
            user=user,
        )

    # route == LOCAL
    complete = complete or _default_complete()
    return _run_local(
        task_type, criteria, payload, role, patience,
        verify=verify, complete=complete, max_retries=max_retries, user=user,
    )


def _apply_governor(
    config: Config,
    user: str,
    dial: tuple[int, str | None],
    govern: Callable[..., Any] | None,
) -> tuple[int, str | None] | None:
    """Consult the budget governor for ``user`` and return its recommended dial
    for THIS request (or None to leave the dial unchanged). The governor emits
    its own telemetry event on a threshold crossing; we never write the dial
    state file here — the tightening is per-request only."""
    governor = govern
    if governor is None:
        from ..econ.governor import govern_user as governor  # noqa: PLC0415 — lazy (econ optional at import)
    try:
        decision = governor(config, user, dial)
    except Exception:  # noqa: BLE001 — the governor must never break a completion
        return None
    to_dial = getattr(decision, "to_dial", None)
    return to_dial


def _run_local(
    task_type: str,
    criteria: str,
    payload: str,
    role: str,
    patience: str,
    *,
    verify: VerifyFn | None,
    complete: CompleteFn,
    max_retries: int,
    user: str,
) -> dict[str, Any]:
    """The local verify-and-repair loop. One ``generation`` event per attempt +
    a ``task.run`` summary."""
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
        # This is the terminal attempt when the run has spent its whole budget.
        # A local run that ends not-ok on its terminal attempt IS the escalation
        # (the caller must step up a tier), so exactly that one generation event
        # carries escalated=True; a run that succeeds earlier breaks the loop and
        # never reaches an escalated=True emission.
        is_last = attempts >= max_attempts
        attempt_started = time.monotonic()
        prompt = _build_prompt(criteria, payload, feedback)
        attempt_ok = False
        attempt_error: str | None = None
        try:
            raw = complete(prompt, system=ENVELOPE_SYSTEM, role=role)
        except Exception as exc:  # noqa: BLE001 — a model/backend failure is a repairable attempt
            error_kind = attempt_error = type(exc).__name__
            feedback = f"model call failed: {error_kind}"
            _emit_generation(
                task_type, "local", role, user,
                retries=attempts - 1, escalated=is_last, ok=False,
                duration_s=round(time.monotonic() - attempt_started, 3),
                error_kind=attempt_error,
            )
            continue
        last_env = _parse_envelope(_completion_text(raw))
        tokens_in, tokens_out = _completion_tokens(raw)
        if verify is None:
            ok = attempt_ok = True
        else:
            accepted, fb = verify(_as_text(last_env["output"]), criteria)
            if accepted:
                ok = attempt_ok = True
            else:
                error_kind = attempt_error = "verify_failed"
                feedback = fb
        _emit_generation(
            task_type, "local", role, user,
            retries=attempts - 1, escalated=(is_last and not attempt_ok), ok=attempt_ok,
            duration_s=round(time.monotonic() - attempt_started, 3),
            error_kind=attempt_error, tokens_in=tokens_in, tokens_out=tokens_out,
        )
        if attempt_ok:
            break

    retries = attempts - 1
    escalated = not ok
    duration_s = round(time.monotonic() - started, 3)
    telemetry.log_event(
        "task.run",
        task_type=task_type,
        route="local",
        model=role,
        user=user,
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
        "error_kind": error_kind if not ok else None,
        "reminder": "caller verifies output vs criteria; escalated=True means escalate to a stronger tier",
    }


def _run_cloud(
    task_type: str,
    criteria: str,
    payload: str,
    config: Config,
    role: str,
    patience: str,
    *,
    verify: VerifyFn | None,
    cloud_dispatch: CloudDispatchFn,
    max_retries: int,
    user: str,
) -> dict[str, Any]:
    """Dispatch a cloud route through the cloud adapter, with the same
    verify-and-repair loop. A dispatch that cannot proceed (no enabled provider,
    no key, provider failure) is a hard :class:`CloudUnavailable` — never a
    silent local fallback. One ``generation`` event per attempt + a ``task.run``
    summary."""
    started = time.monotonic()
    attempts = 0
    max_attempts = max_retries + 1
    feedback: str | None = None
    last_env: dict[str, Any] = {"output": None, "self_confidence": None, "criteria_met": None}
    ok = False
    error_kind: str | None = None
    model_used: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    while attempts < max_attempts:
        attempts += 1
        attempt_started = time.monotonic()
        prompt = _build_prompt(criteria, payload, feedback)
        try:
            result = cloud_dispatch(config, role, prompt, system=ENVELOPE_SYSTEM)
        except Exception as exc:  # noqa: BLE001 — a cloud dispatch failure is HARD, no local fallback
            # A provider/config/key failure is fail-closed: emit the failed
            # attempt, then re-raise as CloudUnavailable — never downgrade local.
            # cloud events are costed via cloud_runs; escalated stays False to
            # keep the receipt categories disjoint (a cloud event tagged escalated
            # would be double-counted as cloud-hitting spend).
            _emit_generation(
                task_type, "cloud", role, user,
                retries=attempts - 1, escalated=False, ok=False,
                duration_s=round(time.monotonic() - attempt_started, 3),
                error_kind=type(exc).__name__,
            )
            telemetry.log_event(
                "task.run", task_type=task_type, route="cloud", model=role, user=user,
                retries=attempts - 1, escalated=True, ok=False,
                duration_s=round(time.monotonic() - started, 3),
                error_kind=type(exc).__name__,
            )
            raise CloudUnavailable(str(exc)) from exc

        model_used = getattr(result, "model", None) or role
        tokens_in = getattr(result, "tokens_in", None)
        tokens_out = getattr(result, "tokens_out", None)
        raw = getattr(result, "text", None)
        last_env = _parse_envelope(raw if isinstance(raw, str) else _as_text(raw))
        attempt_ok = False
        attempt_error: str | None = None
        if verify is None:
            ok = attempt_ok = True
        else:
            accepted, fb = verify(_as_text(last_env["output"]), criteria)
            if accepted:
                ok = attempt_ok = True
            else:
                error_kind = attempt_error = "verify_failed"
                feedback = fb
        # cloud events are costed via cloud_runs; escalated stays False to keep
        # the receipt categories disjoint (a cloud event tagged escalated would
        # be double-counted as cloud-hitting spend).
        _emit_generation(
            task_type, "cloud", model_used, user,
            retries=attempts - 1, escalated=False, ok=attempt_ok,
            duration_s=round(time.monotonic() - attempt_started, 3),
            error_kind=attempt_error, tokens_in=tokens_in, tokens_out=tokens_out,
        )
        if attempt_ok:
            break

    retries = attempts - 1
    escalated = not ok
    duration_s = round(time.monotonic() - started, 3)
    telemetry.log_event(
        "task.run",
        task_type=task_type,
        route="cloud",
        model=model_used or role,
        user=user,
        retries=retries,
        escalated=escalated,
        ok=ok,
        duration_s=duration_s,
        error_kind=error_kind if not ok else None,
    )
    return {
        "task_type": task_type,
        "route": "cloud",
        "role": role,
        "model": model_used,
        "output": last_env["output"],
        "self_confidence": last_env.get("self_confidence"),
        "criteria_met": last_env.get("criteria_met"),
        "ok": ok,
        "retries": retries,
        "escalated": escalated,
        "escalation_patience": patience,
        "duration_s": duration_s,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "error_kind": error_kind if not ok else None,
        "reminder": "caller verifies output vs criteria; escalated=True means escalate to a stronger tier",
    }


def _emit_generation(
    task_type: str,
    route: str,
    model: str,
    user: str,
    *,
    retries: int,
    escalated: bool,
    ok: bool,
    duration_s: float,
    error_kind: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Emit one content-free ``kind="generation"`` event — the costable unit the
    econ report and budget governor consume (fields per the ARCHITECTURE
    contract). Token counts are included when the provider reports them."""
    fields: dict[str, Any] = {
        "task_type": task_type,
        "route": route,
        "model": model,
        "user": user,
        "retries": retries,
        "escalated": escalated,
        "ok": ok,
        "duration_s": duration_s,
    }
    if error_kind is not None:
        fields["error_kind"] = error_kind
    if tokens_in is not None:
        fields["tokens_in"] = tokens_in
    if tokens_out is not None:
        fields["tokens_out"] = tokens_out
    telemetry.log_event("generation", **fields)
