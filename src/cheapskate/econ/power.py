# SPDX-License-Identifier: Apache-2.0
"""Local power draw, honestly measured or honestly omitted.

Three modes, in preference order:

  1. **Measured** — on Apple Silicon macOS, ``powermetrics`` reports package
     power in milliwatts. It requires root, so we NEVER invoke sudo ourselves:
     the caller supplies a ``runner`` that already has the privilege (or a test
     fake). If the runner returns usable output we parse watts from it.
  2. **Static estimate** — a ``watts_estimate`` from config (no sudo, no probe).
     Reported clearly as an estimate, not a measurement.
  3. **Unknown** — neither available ⇒ power draw is ``None`` and the cost engine
     OMITS energy cost rather than guessing. "electricity unknown" mode.

Every subprocess call is injected (``runner=``) so tests never spawn a process,
never touch sudo, and never depend on the host OS.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from typing import Callable

# runner(cmd: list[str]) -> str : returns stdout text (may raise on failure).
Runner = Callable[[list[str]], str]

# powermetrics emits e.g. "Combined Power (CPU + GPU + ANE): 21450 mW" and
# "Package Power: 24500 mW". We prefer package/combined power in milliwatts.
_MW_LINE = re.compile(
    r"(?:combined power|package power|cpu power|system.*power)[^\d]*(\d+(?:\.\d+)?)\s*mw",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PowerReading:
    """A power-draw reading with its provenance.

    ``watts`` is None in unknown mode (energy cost must be omitted, not guessed).
    ``mode`` ∈ measured | estimate | unknown. ``detail`` is a short human note.
    """

    watts: float | None
    mode: str
    detail: str

    @property
    def known(self) -> bool:
        return self.watts is not None


def is_apple_silicon(*, system: str | None = None, machine: str | None = None) -> bool:
    """True on macOS running an arm64 (Apple Silicon) CPU. Args injectable for
    tests; default to the live platform."""
    sysname = system if system is not None else platform.system()
    mach = machine if machine is not None else platform.machine()
    return sysname == "Darwin" and mach.lower() in {"arm64", "aarch64"}


def parse_powermetrics_watts(text: str) -> float | None:
    """Extract package/combined power in WATTS from powermetrics text output.
    Returns None if no power line is found."""
    best_mw: float | None = None
    for m in _MW_LINE.finditer(text or ""):
        mw = float(m.group(1))
        # Prefer the largest package-level figure seen (combined ≥ subcomponents).
        if best_mw is None or mw > best_mw:
            best_mw = mw
    return None if best_mw is None else round(best_mw / 1000.0, 3)


def _powermetrics_cmd() -> list[str]:
    # -n 1: one sample; -i 200: 200 ms window; --samplers cpu_power for the power
    # lines only. The caller's runner is responsible for any sudo prefix — we do
    # not add one. This is the command shape only; nothing is executed here.
    return ["powermetrics", "-n", "1", "-i", "200", "--samplers", "cpu_power"]


def read_power(
    *,
    watts_estimate: float | None = None,
    runner: Runner | None = None,
    allow_measure: bool = False,
    system: str | None = None,
    machine: str | None = None,
) -> PowerReading:
    """Resolve the current power draw.

    * ``allow_measure=True`` AND a ``runner`` AND Apple Silicon ⇒ try to measure
      via powermetrics (the runner owns the privilege; we never call sudo).
    * else if ``watts_estimate`` is set ⇒ static estimate mode.
    * else ⇒ unknown mode (watts=None; energy cost omitted downstream).

    ``allow_measure`` defaults to False so no code path — and no test — ever
    triggers a privileged probe unless the caller explicitly opts in with a
    runner it controls.
    """
    if allow_measure and runner is not None and is_apple_silicon(system=system, machine=machine):
        try:
            out = runner(_powermetrics_cmd())
            watts = parse_powermetrics_watts(out)
            if watts is not None:
                return PowerReading(watts=watts, mode="measured", detail="powermetrics")
        except Exception as exc:  # noqa: BLE001 — a failed probe degrades, never crashes
            if watts_estimate is not None:
                return PowerReading(
                    watts=float(watts_estimate),
                    mode="estimate",
                    detail=f"powermetrics failed ({type(exc).__name__}); using config estimate",
                )
            return PowerReading(
                watts=None,
                mode="unknown",
                detail=f"powermetrics failed ({type(exc).__name__}); no estimate configured",
            )

    if watts_estimate is not None:
        return PowerReading(
            watts=float(watts_estimate),
            mode="estimate",
            detail="config watts_estimate (no measurement taken)",
        )
    return PowerReading(
        watts=None,
        mode="unknown",
        detail="power draw unknown; set econ.watts_estimate or enable measurement",
    )
