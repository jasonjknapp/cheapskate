# SPDX-License-Identifier: Apache-2.0
"""The pure routing decision: given a task-type and the dial, decide whether the
work goes local, cloud, or is refused outright.

Two symmetric fail-closed classes, both enforced HERE before any dial logic:

  * ``never_local`` — the task must not be answered by a local model, and there
    is NO silent cloud fallback. It resolves to a hard refusal
    (``route="refuse-never-local"``); the caller turns that into a
    :class:`NeverLocal` exception rather than routing around it.
  * ``never_cloud`` — the task must never leave the machine. If the dial would
    otherwise send it to the cloud, that is a hard error
    (``route="refuse-never-cloud"``) — the work is kept local or refused, never
    shipped off-box.

``route_decision`` is a pure function: same inputs, same output, no I/O.
"""

from __future__ import annotations

from typing import Any

from ..config import Config

# route values the decision can return
LOCAL = "local"
CLOUD = "cloud"
CLOUD_DOWNGRADED = "cloud-downgraded"  # local-capable route, but dial sent it cloud
REFUSE_NEVER_LOCAL = "refuse-never-local"
REFUSE_NEVER_CLOUD = "refuse-never-cloud"
UNKNOWN = "unknown-task-type"


class NeverLocal(Exception):
    """A never-local task-type was routed — a hard policy refusal, no fallback."""


class NeverCloud(Exception):
    """A never-cloud task-type would have left the machine — hard refusal."""


def _classify(task_type: str, config: Config) -> str | None:
    """Return the fail-closed class of a task-type: 'never_local', 'never_cloud',
    or None. never_local wins if a type is (mis)listed in both — refusing is the
    safe direction."""
    if task_type in config.never_local:
        return "never_local"
    if task_type in config.never_cloud:
        return "never_cloud"
    return None


def route_decision(task_type: str, dial: tuple[int, str | None], config: Config) -> dict[str, Any]:
    """Decide the route for ``task_type`` at ``dial`` = ``(level, sub_dial)``.

    Pure. Never raises — a never-local/never-cloud violation is returned AS a
    refuse route (the caller raises). Returns a dict with at least ``task_type``,
    ``route``, and ``reason``; local/cloud routes also carry ``role``, ``tier``,
    ``min_level``, and ``escalation_patience``.
    """
    level, sub_dial = dial
    dial_str = _dial_str(level, sub_dial)

    klass = _classify(task_type, config)
    if klass == "never_local":
        return {
            "task_type": task_type,
            "route": REFUSE_NEVER_LOCAL,
            "reason": "policy: never_local — top-tier only, no local answer and no silent fallback",
            "dial": dial_str,
        }
    if klass == "never_cloud":
        # never_cloud is only violated if the route would go OFF the machine. At
        # any dial we keep it local; if local is impossible (level 0 cloud-first)
        # we refuse rather than ship it off-box.
        if level == 0:
            return {
                "task_type": task_type,
                "route": REFUSE_NEVER_CLOUD,
                "reason": "policy: never_cloud — must stay on-machine, but dial level 0 is cloud-first",
                "dial": dial_str,
            }
        route_cfg = config.task_types.get(task_type)
        role = route_cfg.role if route_cfg else "reasoning"
        return {
            "task_type": task_type,
            "route": LOCAL,
            "role": role,
            "reason": "policy: never_cloud — forced local",
            "dial": dial_str,
            "escalation_patience": _patience(level, sub_dial),
        }

    route_cfg = config.task_types.get(task_type)
    if route_cfg is None:
        return {
            "task_type": task_type,
            "route": UNKNOWN,
            "reason": "no route registered; the caller should handle it",
            "dial": dial_str,
        }

    base = route_cfg.route
    role = route_cfg.role
    tier = route_cfg.tier
    min_level = route_cfg.min_level

    # A cloud-only task-type always routes cloud (it never had a local option).
    if base == "cloud-only":
        return {
            "task_type": task_type,
            "route": CLOUD,
            "role": role,
            "tier": tier,
            "min_level": min_level,
            "reason": "cloud-only task-type",
            "dial": dial_str,
        }

    # local-first: decide by dial.
    #   level 0 → cloud-first: send cloud (downgraded from its local capability)
    #   level 3 → local-only:  always local
    #   level 1/2 → local when level >= route floor; a level-2 lite sub-dial
    #               only routes clearly-safe ('safe' tier) types locally.
    go_local, why = _local_gate(level, sub_dial, tier, min_level)

    out: dict[str, Any] = {
        "task_type": task_type,
        "role": role,
        "tier": tier,
        "min_level": min_level,
        "dial": dial_str,
    }
    if go_local:
        out["route"] = LOCAL
        out["reason"] = f"local-first via role:{role}"
        out["escalation_patience"] = _patience(level, sub_dial)
    else:
        out["route"] = CLOUD_DOWNGRADED
        out["reason"] = why
    return out


def _local_gate(level: int, sub_dial: str | None, tier: str, min_level: int) -> tuple[bool, str]:
    """Return ``(go_local, reason_if_not)``."""
    if level == 0:
        return (False, "dial level 0 (cloud-first) sends local-capable work to the cloud")
    if level == 3:
        return (True, "")
    # levels 1 and 2
    if level < min_level:
        return (False, f"dial below route floor (needs level {min_level}+)")
    if level == 2 and sub_dial == "lite" and tier != "safe":
        return (False, "dial level 2 lite routes only clearly-safe task-types locally")
    return (True, "")


def _patience(level: int, sub_dial: str | None) -> str:
    """Signal to the caller's verify-and-repair loop. level-2 max tolerates a
    retry before escalating; everything else escalates fast."""
    return "tolerate-retry" if (level == 2 and sub_dial == "max") else "escalate-fast"


def _dial_str(level: int, sub_dial: str | None) -> str:
    return f"{level}:{sub_dial}" if (level == 2 and sub_dial) else str(level)
