# SPDX-License-Identifier: Apache-2.0
"""Memory-capacity decisions — pure, testable, and fail-closed.

:func:`capacity_decision` decides whether an OLLAMA load may proceed (MLX loads
are gated inside the backend lifecycle itself). It is a pure function so the
policy is unit-tested in isolation. The secondary desktop runtime is preempted
ONLY under memory pressure; a single model larger than the whole budget is
refused (fail-closed) rather than risking a Metal panic.
"""

from __future__ import annotations

from typing import Any, Optional

from ..backends import (
    lms_loaded,
    ollama_resident_gb,
)


def capacity_decision(
    needed_gb: float,
    ollama_resident_gb: float,
    lms_loaded: bool,
    budget_gb: float,
    *,
    model_resident: bool = False,
    lms_gb: Optional[float] = None,
) -> tuple[str, str]:
    """Decide whether an OLLAMA load may proceed. Pure → unit-testable.

    The secondary desktop runtime is preempted only under memory pressure;
    ``lms_gb`` is its measured footprint (None while loaded ⇒ unknown size ⇒
    conservative preempt, safety over politeness).

    Returns ``(action, reason)`` where ``action`` is one of:
      * ``"ok"``                  — headroom exists (or model already resident).
      * ``"evict-lms"``           — memory pressure: preempt the secondary
        runtime, then load.
      * ``"ok-selfevict"``        — over the soft budget, but Ollama will
        LRU-evict its own.
      * ``"503"``                 — a single model larger than the whole budget:
        refuse.
    """
    needed = float(needed_gb or 0)
    if model_resident or needed <= 0:
        return ("ok", "resident-or-unsized")
    if needed > budget_gb:
        return ("503", f"model {needed:.0f}GB alone exceeds RAM budget {budget_gb:.0f}GB")
    projected = needed + float(ollama_resident_gb or 0)
    if lms_loaded:
        if lms_gb is not None and projected + float(lms_gb) <= budget_gb:
            return (
                "ok",
                f"projected {projected:.0f}GB + secondary {float(lms_gb):.0f}GB "
                f"<= budget {budget_gb:.0f}GB — coexist (no pressure)",
            )
        return (
            "evict-lms",
            "memory pressure (or unknown secondary-runtime footprint) — "
            "preempt secondary runtime (only when needed), then load",
        )
    if projected <= budget_gb:
        return ("ok", f"projected {projected:.0f}GB <= budget {budget_gb:.0f}GB")
    return (
        "ok-selfevict",
        f"projected {projected:.0f}GB > budget {budget_gb:.0f}GB; ollama LRU-evicts its own",
    )


def memory_snapshot(budget_gb: float, *, config: Any = None) -> dict:
    """Live memory ledger for the status endpoint. Never raises."""
    return {
        "ram_budget_gb": budget_gb,
        "ollama_resident_gb": round(ollama_resident_gb(), 1),
        "secondary_runtime_loaded": lms_loaded(),
    }
