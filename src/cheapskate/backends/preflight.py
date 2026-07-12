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
Ollama models, then re-checks, and RAISES if still over budget (fail-closed
beats a Metal panic).
"""

from __future__ import annotations

import sys
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
        return  # everything coexists, no eviction
    if lms_hot:
        say(f"[coresident] memory pressure ({needed:.0f} GB load), de-loading secondary runtime first")
        lms_unload()
    if over():
        say(f"[coresident] ollama residency + {needed:.0f} GB exceeds {budget_gb:.0f} GB, stopping ollama models")
        ollama_stop()
        sleep(settle_s)
    if over():
        raise RuntimeError(
            f"co-resident models still exceed RAM budget after eviction "
            f"(need {needed:.0f} GB + ollama {ollama_resident():.0f} GB > {budget_gb:.0f} GB) "
            f", refusing to load"
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


def _auto_pull_enabled(config: Any) -> bool:
    """Whether fetch-on-demand is enabled (``machine.auto_pull``, default True)."""
    machine = _get(config, "machine", {}) or {}
    val = _get(machine, "auto_pull", True)
    return bool(val) if val is not None else True


def _machine_num(config: Any, key: str, default: float) -> float:
    machine = _get(config, "machine", {}) or {}
    val = _get(machine, key, default)
    try:
        return float(val) if val is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access, so a pydantic Config OR a plain dict both work."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def ensure_role(
    role: Optional[str] = None,
    model: Optional[str] = None,
    *,
    config: Any = None,
    budget_gb: float,
    ensure: Callable[..., str] = ensure_mlx,
    pull: Callable[..., bool] = ollama.ollama_pull,
    model_present: Callable[[str], bool] = ollama.ollama_model_present,
    free_disk: Callable[[], float] = None,  # type: ignore[assignment]
    log: Optional[Callable[[str], None]] = None,
) -> BackendSpec:
    """Ensure the role/model is served and return its resolved :class:`BackendSpec`.

    * MLX → drives the single-large-model lifecycle (de-load co-residents,
      flock, spawn) via ``ensure``. A missing MLX model is NOT auto-fetched here
      (an HF snapshot is more involved than a one-liner); the error names the
      acquisition path. (Follow-up: MLX fetch-on-demand.)
    * Ollama → validates presence; when the model is not yet pulled AND
      ``machine.auto_pull`` is on, it fetches on demand via ``ollama pull`` ,
      GATED by the same fail-closed disk/size/RAM budget as model currency
      (:func:`registry.currency.candidate_fits`). If the gate refuses (too big,
      disk headroom, undeterminable size), it does NOT pull and raises
      :class:`LocalUnavailable` with the gate's reason.

    ``budget_gb`` is the RAM budget (ram_gb - headroom). Every side-effecting
    collaborator (``ensure``, ``pull``, ``model_present``, ``free_disk``) is
    injectable so tests drive the decision path without a real server or a real
    download.
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
        # Fast path: already resident (loaded) OR already pulled (Ollama's daemon
        # auto-loads a pulled model on request), so nothing to fetch.
        if ollama.ollama_model_resident(spec.model) or model_present(spec.model):
            return spec
        if not _auto_pull_enabled(config):
            raise LocalUnavailable(
                f"ollama model {spec.model!r} (role {role}) not present and "
                f"auto_pull is disabled: run `ollama pull {spec.model}` (or set "
                f"machine.auto_pull: true)"
            )
        _auto_pull_ollama(spec, config=config, budget_gb=budget_gb,
                          pull=pull, model_present=model_present,
                          free_disk=free_disk, log=log)
    return spec


def _auto_pull_ollama(
    spec: BackendSpec,
    *,
    config: Any,
    budget_gb: float,
    pull: Callable[..., bool],
    model_present: Callable[[str], bool],
    free_disk: Optional[Callable[[], float]],
    log: Optional[Callable[[str], None]],
) -> None:
    """Fetch an absent Ollama model on demand, GATED by the fail-closed
    disk/size/RAM budget. Raises :class:`LocalUnavailable` if the gate refuses or
    the pull fails, never bypasses the gate (a freelance multi-GB pull that
    fills the disk is exactly what the gate prevents)."""
    from ..registry.currency import candidate_fits, free_disk_gb

    say = log or (lambda msg: print(msg, file=sys.stderr))
    disk_probe = free_disk or free_disk_gb
    try:
        free_gb = float(disk_probe())
    except Exception as exc:  # noqa: BLE001, undeterminable free disk fails closed
        raise LocalUnavailable(
            f"ollama model {spec.model!r} not present and free disk is "
            f"undeterminable ({type(exc).__name__}), refusing to pull (fail-closed)"
        ) from exc

    ram_headroom = _machine_num(config, "ram_headroom_gb", 24.0)
    disk_headroom = _machine_num(config, "disk_headroom_gb", 15.0)
    # ``budget_gb`` is the net RAM budget (ram_gb - headroom); 0.0 means RAM is
    # unknown, which must fail closed. candidate_fits gates on
    # (ram_budget_gb - ram_headroom_gb), so pass (budget_gb + headroom) back to
    # recover exactly ``budget_gb`` as the effective ceiling.
    if not budget_gb:
        raise LocalUnavailable(
            f"ollama model {spec.model!r} not present and the RAM budget is "
            f"unknown (0GB), refusing to pull (fail-closed)"
        )
    # The candidate's size: the role's approx_gb (a tag-only backend has no size
    # API). candidate_fits fails closed if this is None.
    ok, reason, size = candidate_fits(
        spec.model,
        "ollama",
        free_disk_gb=free_gb,
        ram_budget_gb=budget_gb + ram_headroom,
        disk_headroom_gb=disk_headroom,
        ram_headroom_gb=ram_headroom,
        assume_size_gb=spec.approx_gb,
    )
    if not ok:
        raise LocalUnavailable(
            f"ollama model {spec.model!r} not present; auto-pull refused by the "
            f"disk/RAM gate: {reason}"
        )
    say(
        f"role {spec.role}: {spec.model} not present, downloading "
        f"~{size:.0f}GB (fits: disk ok, ram ok)"
    )
    if not pull(spec.model):
        raise LocalUnavailable(
            f"ollama pull of {spec.model!r} failed, the model is still not "
            f"present (check `ollama pull {spec.model}` manually)"
        )
    if not model_present(spec.model):
        raise LocalUnavailable(
            f"ollama pull of {spec.model!r} reported success but the model is "
            f"still not present"
        )


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
