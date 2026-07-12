# SPDX-License-Identifier: Apache-2.0
"""Model-currency engine: keep each role's model current without letting anything
freelance multi-GB downloads or deletions.

Pipeline per role:  discover → evaluate → promote → prune

  * discover : query the model hub for newer models from allowlisted publishers;
               shortlist same-lineage candidates. Network failures degrade to
               ``[]`` (never raises). The hub client is injectable (``api=``) so
               tests never touch the network.
  * evaluate : size the CANDIDATE (not the incumbent) and fail closed BEFORE any
               download if the size is undeterminable, exceeds the max, would
               breach disk headroom, or exceeds the RAM budget. Then run the
               caller's eval suite and decide.
  * promote  : atomic registry swap, retaining the old incumbent as a rollback.
  * prune    : an ALLOWLIST, never a keep-set — only ever delete models this
               engine downloaded AND has since superseded. An incumbent, a
               fallback, a retained rollback, or a model this engine never
               managed is NEVER a prune candidate.

Sizing a candidate before download is the load-bearing fix: gating on the
incumbent's size would let a candidate far larger than the incumbent sail past
the disk check and fill the volume mid-download.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Callable, Protocol

from . import registry as _registry


# ── hub client protocol (injectable) ─────────────────────────────────────────


class HubApi(Protocol):
    """The minimal slice of a model-hub client this engine uses. A real
    ``huggingface_hub.HfApi`` satisfies it; tests pass a fake."""

    def list_models(self, *, author: str, sort: str, limit: int) -> list[Any]: ...

    def model_info(self, repo: str, *, files_metadata: bool) -> Any: ...


# ── lineage ──────────────────────────────────────────────────────────────────


def same_lineage(incumbent: str | None, candidate: str | None) -> bool:
    """Share a model-family token (e.g. ``qwen3`` / ``llama3`` / ``gemma``)."""

    def fam(m: str | None) -> str:
        base = (m or "").split("/")[-1].lower()
        mt = re.match(r"[a-z]+[0-9.]*", base)
        return mt.group(0) if mt else base

    return bool(incumbent) and bool(candidate) and fam(incumbent) == fam(candidate)


# ── discovery ────────────────────────────────────────────────────────────────


def discover(
    role: str,
    registry: dict[str, Any],
    publisher_allowlist: list[str],
    *,
    api: HubApi | None = None,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """Shortlist candidate models for ``role`` from allowlisted publishers.

    Returns ``[{repo, author, last_modified, same_lineage}]``. Any hub failure
    degrades to ``[]`` (never raises). ``api`` is injectable for tests; when
    absent it is lazily constructed and a missing client just yields ``[]``.
    """
    incumbent = _registry.incumbent(registry, role)
    if api is None:
        try:
            from huggingface_hub import HfApi  # lazy — only needed here

            api = HfApi()
        except Exception:  # noqa: BLE001 — no hub client ⇒ no discovery
            return []
    out: list[dict[str, Any]] = []
    for author in sorted(set(publisher_allowlist)):
        try:
            models = api.list_models(author=author, sort="lastModified", limit=limit)
        except Exception:  # noqa: BLE001 — one bad author must not kill discovery
            continue
        for m in models:
            repo = getattr(m, "id", None) or getattr(m, "modelId", None)
            if not repo:
                continue
            out.append(
                {
                    "repo": repo,
                    "author": author,
                    "last_modified": str(getattr(m, "lastModified", "")),
                    "same_lineage": same_lineage(incumbent, repo),
                }
            )
    return out


# ── candidate sizing + fit gate (fail-closed, sizes the CANDIDATE) ───────────


def candidate_size_gb(candidate: str, backend: str, *, api: HubApi | None = None,
                      known_sizes: dict[str, float] | None = None) -> float | None:
    """Best-effort size of a CANDIDATE, in GB, BEFORE any download.

    Hub repos (id contains ``/``): sum ``siblings[].size`` from
    ``model_info(repo, files_metadata=True)``. Tag-only backends have no size
    API, so look the tag up in ``known_sizes``. Returns None when undeterminable
    (the caller fails closed)."""
    known_sizes = known_sizes or {}
    if backend in ("ollama", "lmstudio") or "/" not in candidate:
        v = known_sizes.get(candidate)
        return float(v) if v is not None else None
    try:
        if api is None:
            from huggingface_hub import HfApi  # lazy

            api = HfApi()
        info = api.model_info(candidate, files_metadata=True)
        total, any_size = 0, False
        for s in getattr(info, "siblings", None) or []:
            sz = getattr(s, "size", None)
            if sz:
                total += sz
                any_size = True
        return (total / 1e9) if any_size else None
    except Exception:  # noqa: BLE001 — unknown size ⇒ fail closed at the gate
        return None


def candidate_fits(
    candidate: str,
    backend: str,
    *,
    free_disk_gb: float,
    ram_budget_gb: float | None,
    disk_headroom_gb: float = 15.0,
    ram_headroom_gb: float = 24.0,
    max_download_gb: float = 80.0,
    assume_size_gb: float | None = None,
    api: HubApi | None = None,
    known_sizes: dict[str, float] | None = None,
) -> tuple[bool, str, float | None]:
    """Fail-closed pre-download budget gate for a CANDIDATE.

    Returns ``(ok, reason, size_gb)``. Refuses if the size is undeterminable
    (unless ``assume_size_gb`` is given), exceeds ``max_download_gb``, would
    breach disk headroom, or exceeds the RAM budget minus headroom.
    """
    size = (
        assume_size_gb
        if assume_size_gb is not None
        else candidate_size_gb(candidate, backend, api=api, known_sizes=known_sizes)
    )
    if size is None:
        return (False, "candidate size undeterminable (fail-closed; pass assume_size_gb to override)", None)
    if size > max_download_gb:
        return (False, f"candidate {size:.0f}GB exceeds max_download_gb {max_download_gb:.0f}GB", size)
    if (free_disk_gb - size) < disk_headroom_gb:
        return (
            False,
            f"disk headroom: {free_disk_gb:.0f}GB free - {size:.0f}GB candidate < {disk_headroom_gb:.0f}GB",
            size,
        )
    if ram_budget_gb is not None and size > (ram_budget_gb - ram_headroom_gb):
        return (False, f"candidate {size:.0f}GB exceeds RAM budget ({ram_budget_gb}-{ram_headroom_gb}GB)", size)
    return (True, "within budget", size)


def free_disk_gb(path: Path | None = None) -> float:
    """Free GB on the volume backing ``path`` (home if unspecified)."""
    target = path if path is not None else Path.home()
    return shutil.disk_usage(target).free / 1e9


# ── evaluate ─────────────────────────────────────────────────────────────────

# An eval suite: (model, backend) -> summary dict with at least
# {pass_rate, critical_passed, critical_total}. The engine stays agnostic to how
# scoring works; the caller supplies it.
EvalFn = Callable[[str, str], dict[str, Any]]
# A promotion decision: (incumbent_summary, candidate_summary, same_lineage) ->
# {promote: bool, reason: str}. Enforces the caller's critical floor + margin.
DecisionFn = Callable[[dict[str, Any], dict[str, Any], bool], dict[str, Any]]


def evaluate(
    role: str,
    candidate: str,
    registry: dict[str, Any],
    *,
    eval_fn: EvalFn,
    decision_fn: DecisionFn,
    fits: bool = True,
    fit_reason: str = "within budget",
    candidate_size_gb: float | None = None,
    quarantined_ok: bool = False,
) -> dict[str, Any]:
    """Evaluate ``candidate`` against ``role``'s incumbent.

    The disk/RAM gate is decided by the CALLER (via :func:`candidate_fits`) and
    passed in as ``fits`` — evaluate fails closed BEFORE running the suite if the
    candidate does not fit or is quarantined. Returns a plan dict with a
    ``decision`` = ``{promote, reason}``.
    """
    rc = _registry.get_role(registry, role)
    if not rc or not rc.get("model"):
        raise ValueError(f"role {role!r} has no incumbent in the registry")
    incumbent = rc["model"]
    inc_backend = rc.get("backend", "ollama")
    cand_backend = rc.get("backend", inc_backend)  # candidate assumed same backend family
    lineage = same_lineage(incumbent, candidate)

    plan: dict[str, Any] = {
        "role": role,
        "incumbent": incumbent,
        "candidate": candidate,
        "same_lineage": lineage,
        "candidate_size_gb": candidate_size_gb,
        "fits": fits,
    }
    if not quarantined_ok and _registry.is_quarantined(registry, role, candidate):
        plan["decision"] = {"promote": False, "reason": "candidate is quarantined (known-bad)"}
        return plan
    if not fits:
        plan["decision"] = {"promote": False, "reason": f"pre-download gate: {fit_reason}"}
        return plan

    try:
        inc_summary = eval_fn(incumbent, inc_backend)
        cand_summary = eval_fn(candidate, cand_backend)
    except Exception as exc:  # noqa: BLE001 — a model that errors fails the eval, not the engine
        plan["decision"] = {"promote": False, "reason": f"evaluation error: {exc}"}
        return plan

    plan["incumbent_summary"] = inc_summary
    plan["candidate_summary"] = cand_summary
    plan["decision"] = decision_fn(inc_summary, cand_summary, lineage)
    return plan


# ── promote / rollback (delegate to the registry's atomic swap) ──────────────


def promote(
    role: str,
    candidate: str,
    backend: str,
    registry: dict[str, Any],
    *,
    dry_run: bool = True,
    endpoint: str | None = None,
    approx_gb: float | None = None,
    fallback: str | None = None,
    prune: str | None = None,
) -> dict[str, Any]:
    """Swap ``role`` to ``candidate``, retaining the old incumbent as a rollback.
    Mutates ``registry`` in place when applied (caller persists via
    ``registry.save``). ``dry_run`` (default) reports the plan without mutating.
    """
    prev = _registry.incumbent(registry, role)
    plan = {"role": role, "from": prev, "to": candidate, "backend": backend, "applied": False}
    if dry_run:
        plan["reason"] = "dry-run: registry not mutated"
        return plan
    _registry.set_incumbent(
        registry, role, candidate, backend,
        endpoint=endpoint, approx_gb=approx_gb, fallback=fallback, prune=prune,
    )
    plan["applied"] = True
    return plan


def rollback(role: str, registry: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
    """Restore ``role``'s most recent retained rollback as the incumbent."""
    target = (_registry.get_role(registry, role) or {}).get("rollback") or []
    if not target:
        return {"role": role, "applied": False, "reason": "no rollback retained"}
    plan = {"role": role, "to": target[0], "applied": False}
    if dry_run:
        plan["reason"] = "dry-run"
        return plan
    restored = _registry.rollback(registry, role)
    plan["to"] = restored
    plan["applied"] = restored is not None
    return plan


# ── prune (ALLOWLIST + protected-set guard) ──────────────────────────────────


def prune_candidates(
    registry: dict[str, Any],
    managed: dict[str, dict[str, Any]],
    *,
    keep_n: int = _registry.KEEP_ROLLBACK_N,
) -> list[dict[str, Any]]:
    """Models this engine MAY delete: recorded in ``managed`` AND not a current
    incumbent/fallback/retained-rollback AND ``prune != "never"``. A model this
    engine never managed is NEVER a candidate (allowlist, not keep-set).
    """
    protected = _registry.protected_models(registry, keep_n=keep_n)
    out: list[dict[str, Any]] = []
    for model, meta in managed.items():
        if model in protected:
            continue
        if meta.get("prune") == "never":
            continue
        out.append({"model": model, "backend": meta.get("backend"), "status": meta.get("status")})
    return out
