# SPDX-License-Identifier: Apache-2.0
"""Role preflight: guarantee the right model is served, safely, before a request.

The preflight is the one entry point a consumer calls to be sure the correct
backend is serving the correct model. For MLX it drives the single-large-model
lifecycle (de-load co-residents, flock, spawn); for Ollama it validates
residency only (the daemon auto-loads on request) and NEVER pulls.

``evict_coresidents`` is the cross-runtime memory-safety primitive: before a
large MLX load it de-loads models resident in OTHER runtimes so the combined
footprint can't breach the RAM budget. It de-loads the secondary desktop runtime
first (coexistence policy: preempt only under pressure), then stops resident
Ollama models, then re-checks — and RAISES if still over budget (fail-closed
beats a Metal panic).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from . import ollama
from .mlx import ensure_mlx, mlx_health, stop_mlx, _read_state
from .resolve import BackendSpec, LocalUnavailable, port_of, resolve


def evict_coresidents(
    needed_gb: float,
    budget_gb: float,
    *,
    log: Optional[Callable[[str], None]] = None,
    ollama_resident: Callable[[], float] = ollama.ollama_resident_gb,
    lms_loaded: Callable[[], bool] = ollama.lms_loaded,
    lms_resident: Callable[[], float] = ollama.lms_resident_gb,
    lms_unload: Callable[[], object] = ollama.lms_unload_all,
    ollama_stop: Callable[[], object] = ollama.ollama_stop_all,
    settle_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """De-load co-resident models in other runtimes to fit ``needed_gb``.

    Coexistence is the default: if everything (including the secondary runtime's
    models) fits the budget, nothing is evicted. Under pressure the secondary
    runtime is preempted first, then resident Ollama models are stopped. Still
    over budget after eviction → raise RuntimeError (fail-closed).

    Every side-effecting collaborator is injectable for tests.
    """
    say = log or (lambda *_: None)
    needed = float(needed_gb or 0)

    def over(include_lms: bool = False) -> bool:
        extra = lms_resident() if include_lms else 0.0
        return needed + ollama_resident() + extra > budget_gb

    lms_hot = lms_loaded()
    if not over(include_lms=lms_hot):
        return  # everything coexists — no eviction
    if lms_hot:
        say(f"[coresident] memory pressure ({needed:.0f} GB load) — de-loading secondary runtime first")
        lms_unload()
    if over():
        say(f"[coresident] ollama residency + {needed:.0f} GB exceeds {budget_gb:.0f} GB — stopping ollama models")
        ollama_stop()
        sleep(settle_s)
    if over():
        raise RuntimeError(
            f"co-resident models still exceed RAM budget after eviction "
            f"(need {needed:.0f} GB + ollama {ollama_resident():.0f} GB > {budget_gb:.0f} GB) "
            f"— refusing to load"
        )


def resident_check(spec: BackendSpec) -> bool:
    """True if the correct server is already serving the spec's model (fast path)."""
    if spec.backend == "mlx":
        st = _read_state()
        from .mlx import _pid_alive

        return (
            st.get("model") == spec.model
            and _pid_alive(st.get("pid"))
            and mlx_health(st.get("port", port_of(spec)))
        )
    if spec.backend == "ollama":
        return ollama.ollama_model_resident(spec.model)
    return False


def ensure_role(
    role: Optional[str] = None,
    model: Optional[str] = None,
    *,
    config: Any = None,
    budget_gb: float,
    ensure: Callable[..., str] = ensure_mlx,
) -> BackendSpec:
    """Ensure the role/model is served and return its resolved :class:`BackendSpec`.

    * MLX → drives the single-large-model lifecycle (de-load co-residents,
      flock, spawn) via ``ensure``.
    * Ollama → validates residency only; a missing model raises
      :class:`LocalUnavailable` (this layer NEVER pulls).

    ``budget_gb`` is the RAM budget (ram_gb - headroom). ``ensure`` is injectable
    so tests can drive the decision path without a real server.
    """
    spec = resolve(role=role, model=model, config=config)
    if spec.backend in ("mlx", "mlx_vlm"):
        endpoint = ensure(
            spec.model,
            approx_gb=spec.approx_gb,
            port=port_of(spec),
            budget_gb=budget_gb,
            evict=lambda needed: evict_coresidents(needed, budget_gb),
        )
        return BackendSpec(
            model=spec.model, backend=spec.backend, endpoint=endpoint,
            approx_gb=spec.approx_gb, role=spec.role, quant=spec.quant,
        )
    if spec.backend == "ollama":
        if not ollama.ollama_model_resident(spec.model):
            # Residency is the acquisition layer's job, not the preflight's — an
            # absent Ollama model is a configuration/acquisition error.
            raise LocalUnavailable(
                f"ollama model {spec.model!r} (role {role}) not resident — pull it "
                f"through your model-acquisition path (never a raw pull here)"
            )
    return spec


def release(
    role: Optional[str] = None,
    model: Optional[str] = None,
    *,
    config: Any = None,
    stopper: Callable[[], bool] = stop_mlx,
) -> bool:
    """Stop the role's MLX server (free RAM after a batch). Ollama models are the
    daemon's to manage, so releasing an Ollama role is a no-op. Returns True if a
    server was stopped."""
    spec = resolve(role=role, model=model, config=config)
    if spec.backend in ("mlx", "mlx_vlm"):
        return stopper()
    return False
