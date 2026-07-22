# SPDX-License-Identifier: Apache-2.0
"""Residency probes and eviction helpers for the co-resident local runtimes.

Two runtimes can hold Metal-resident models alongside the MLX server:

  * **Ollama**, the daemon that auto-loads GGUF/quantized models on demand and
    LRU-evicts its own; and
  * a **secondary desktop runtime** (LM Studio, addressed via its ``lms`` CLI)
    that a co-user may have loaded models into.

Before a large MLX load, the preflight accounts for both so the combined
footprint never breaches the RAM budget. The secondary runtime is preempted
ONLY under memory pressure, coexistence is the default; eviction happens only
when it is actually needed.

Every probe is best-effort and never raises: an unknown footprint reads as 0.0
(fail-open here) because the hard stop is the post-eviction re-check in
:func:`cheapskate.backends.preflight.evict_coresidents`.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, Optional

# The secondary desktop runtime's CLI. Override with the CHEAPSKATE_LMS_BIN env
# var if it lives elsewhere; absent binary simply reads as "nothing loaded".
_LMS_BIN = os.environ.get("CHEAPSKATE_LMS_BIN", os.path.expanduser("~/.lmstudio/bin/lms"))

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB)")


def _sum_gb(text: str) -> float:
    """Sum every ``<n> GB|MB`` token in ``ps`` output, normalized to GB."""
    total = 0.0
    for m in _SIZE_RE.finditer(text or ""):
        total += float(m.group(1)) / (1 if m.group(2) == "GB" else 1024)
    return total


def ollama_resident_gb(runner: Optional[Callable[[], str]] = None) -> float:
    """GB currently Metal-resident in the Ollama daemon (via ``ollama ps``).

    Never raises; unknown reads as 0.0 (fail-open, Ollama self-evicts LRU; the
    hard stop is the post-eviction re-check).
    """
    try:
        out = (runner or (lambda: subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout))()
        return _sum_gb(out or "")
    except Exception:  # noqa: BLE001
        return 0.0


def ollama_stop_all(runner: Optional[Callable[[str], object]] = None) -> bool:
    """``ollama stop`` every resident model (they reload on demand). Never raises.

    Returns True if any model was stopped. ``runner`` is injectable for tests.
    """
    try:
        out = subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10
        ).stdout or ""
        names = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()]
        for name in names:
            (runner or (lambda n: subprocess.run(
                ["ollama", "stop", n], capture_output=True, text=True, timeout=30)))(name)
        return bool(names)
    except Exception:  # noqa: BLE001
        return False


def ollama_model_resident(model: str, runner: Optional[Callable[[], str]] = None) -> bool:
    """True if ``model`` is currently loaded in the Ollama daemon. Never raises."""
    try:
        out = (runner or (lambda: subprocess.run(
            ["ollama", "ps"], capture_output=True, text=True, timeout=10).stdout))()
        base = (model or "").split(":")[0]
        return any(
            base and ln.split() and ln.split()[0].split(":")[0] == base
            for ln in (out or "").splitlines()[1:]
        )
    except Exception:  # noqa: BLE001
        return False


def ollama_model_present(model: str, runner: Optional[Callable[[], str]] = None) -> bool:
    """True if ``model`` is PULLED (installed on disk, via ``ollama list``).

    Distinct from :func:`ollama_model_resident`, which checks whether the model
    is currently loaded in RAM (``ollama ps``). Never raises; any error reads as
    "not present" (fail-closed for the "should I pull?" decision)."""
    try:
        out = (runner or (lambda: subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10).stdout))()
        def normalize(name: str) -> str:
            name = (name or "").strip()
            leaf = name.rsplit("/", 1)[-1]
            return name if ":" in leaf else f"{name}:latest"

        wanted = normalize(model)
        return any(
            wanted and ln.split() and normalize(ln.split()[0]) == wanted
            for ln in (out or "").splitlines()[1:]
        )
    except Exception:  # noqa: BLE001
        return False


def ollama_pull(
    model: str,
    *,
    runner: Optional[Callable[[list[str]], "subprocess.CompletedProcess[str]"]] = None,
    timeout: float = 3600.0,
) -> bool:
    """Pull ``model`` via ``ollama pull <model>`` (fetch-on-demand). Returns True
    on success, False on any failure, NEVER raises, so the caller fails closed
    (raises LocalUnavailable) rather than the exception escaping the preflight.

    ``runner`` is injectable for tests (takes the argv list, returns a
    ``CompletedProcess``); the default streams ``ollama``'s own progress to the
    inherited stdout/stderr so a large download is visible."""
    argv = ["ollama", "pull", model]
    try:
        if runner is not None:
            proc = runner(argv)
            return getattr(proc, "returncode", 1) == 0
        proc = subprocess.run(argv, timeout=timeout)  # inherit stdio → live progress
        return proc.returncode == 0
    except Exception:  # noqa: BLE001, a failed pull is False, never an exception
        return False


def lms_loaded(runner: Optional[Callable[[], str]] = None) -> bool:
    """True if the secondary desktop runtime has any model loaded. Never raises."""
    try:
        out = (runner or (lambda: subprocess.run(
            [_LMS_BIN, "ps"], capture_output=True, text=True, timeout=10).stdout))()
        out = (out or "").strip()
        return bool(out) and "no models" not in out.lower()
    except Exception:  # noqa: BLE001
        return False


def lms_resident_gb(runner: Optional[Callable[[], str]] = None) -> float:
    """GB the secondary desktop runtime currently holds (via ``lms ps``).

    Never raises; unknown/none reads as 0.0. Used to decide whether a large load
    can COEXIST with it (preempt only under memory pressure).
    """
    try:
        out = (runner or (lambda: subprocess.run(
            [_LMS_BIN, "ps"], capture_output=True, text=True, timeout=10).stdout))()
        return _sum_gb(out or "")
    except Exception:  # noqa: BLE001
        return 0.0


def lms_unload_all(runner: Optional[Callable[[], object]] = None) -> bool:
    """De-load every secondary-runtime model. Never raises.

    Policy: scheduled/background jobs may preempt the secondary runtime to
    protect the machine, but only when memory actually requires it.
    """
    try:
        (runner or (lambda: subprocess.run(
            [_LMS_BIN, "unload", "--all"], capture_output=True, text=True, timeout=30)))()
        return True
    except Exception:  # noqa: BLE001
        return False
