# SPDX-License-Identifier: Apache-2.0
"""The model registry: ``registry.yaml`` maps roles to their serving choices.

Schema (per role):
    roles:
      <role>:
        model: <repo-or-tag>          # the incumbent
        backend: ollama | mlx | ...
        endpoint: <url>               # optional
        approx_gb: <float>            # optional footprint for fit checks
        fallback: <repo-or-tag>       # optional; served if the incumbent fails
        rollback: [<repo>, ...]       # retained prior incumbents (newest first)
        quarantine: [<repo>, ...]     # known-bad; never promoted to
        prune: managed | never        # deletion policy
        managed_currency: <bool>      # include in the weekly currency pass
        auto: <bool>                  # opt back into eval-gated auto-promote

Writes are ATOMIC (temp file + ``os.replace``) so a crashed run never leaves a
half-written registry. The registry lives in ``state_dir()`` (runtime data,
gitignored) so it stays out of the repo.

The incumbent, the fallback, and every retained rollback are the PROTECTED set —
:func:`protected_models` is what the currency engine consults before deleting
anything.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .. import paths

_REGISTRY_FILE = "registry.yaml"
KEEP_ROLLBACK_N = 1  # rollbacks retained per role


def _registry_path(path: Path | None = None) -> Path:
    return path if path is not None else (paths.state_dir() / _REGISTRY_FILE)


def load(path: Path | None = None) -> dict[str, Any]:
    """Load the registry dict. Missing/empty file ⇒ an empty ``{"roles": {}}``."""
    p = _registry_path(path)
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except OSError:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("registry.yaml must be a mapping")
    raw.setdefault("roles", {})
    return raw


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic on POSIX


def save(registry: dict[str, Any], path: Path | None = None) -> None:
    """Atomically write the registry to ``registry.yaml``."""
    _atomic_write(_registry_path(path), yaml.safe_dump(registry, sort_keys=True))


def get_role(registry: dict[str, Any], role: str) -> dict[str, Any] | None:
    """Return a role's config dict, or None if the role is unregistered."""
    return registry.get("roles", {}).get(role)


def incumbent(registry: dict[str, Any], role: str) -> str | None:
    rc = get_role(registry, role)
    return rc.get("model") if rc else None


def protected_models(registry: dict[str, Any], *, keep_n: int = KEEP_ROLLBACK_N) -> set[str]:
    """Every model that must NEVER be pruned: each role's incumbent, its
    fallback, and its retained rollbacks (newest ``keep_n``). This is the guard
    the currency engine's prune step consults."""
    keep: set[str] = set()
    for rc in registry.get("roles", {}).values():
        if rc.get("model"):
            keep.add(rc["model"])
        if rc.get("fallback"):
            keep.add(rc["fallback"])
        for r in (rc.get("rollback") or [])[:keep_n]:
            keep.add(r)
    return keep


def set_incumbent(
    registry: dict[str, Any],
    role: str,
    model: str,
    backend: str,
    *,
    endpoint: str | None = None,
    approx_gb: float | None = None,
    fallback: str | None = None,
    prune: str | None = None,
    managed_currency: bool | None = None,
    keep_n: int = KEEP_ROLLBACK_N,
) -> dict[str, Any]:
    """Set ``role``'s incumbent to ``model``, retaining the previous incumbent as
    the newest rollback. Mutates and returns ``registry`` (caller persists via
    :func:`save`). Creating a role that does not exist is the sanctioned way to
    register a new one — the registry stays engine-owned.
    """
    roles = registry.setdefault("roles", {})
    rc = roles.setdefault(role, {})
    prev = rc.get("model")
    rc["model"] = model
    rc["backend"] = backend
    if endpoint is not None:
        rc["endpoint"] = endpoint
    if approx_gb is not None:
        rc["approx_gb"] = approx_gb
    if fallback is not None:
        rc["fallback"] = fallback
    if prune is not None:
        rc["prune"] = prune
    if managed_currency is not None:
        rc["managed_currency"] = managed_currency
    if prev and prev != model:
        rollbacks = rc.setdefault("rollback", [])
        rollbacks.insert(0, prev)
        del rollbacks[keep_n:]  # bound retention
    return registry


def quarantine(registry: dict[str, Any], role: str, model: str) -> dict[str, Any]:
    """Mark ``model`` known-bad for ``role`` so it is never promoted to again."""
    rc = registry.setdefault("roles", {}).setdefault(role, {})
    q = rc.setdefault("quarantine", [])
    if model not in q:
        q.append(model)
    return registry


def is_quarantined(registry: dict[str, Any], role: str, model: str) -> bool:
    rc = get_role(registry, role)
    return bool(rc and model in (rc.get("quarantine") or []))


def rollback(registry: dict[str, Any], role: str) -> str | None:
    """Restore the most recent retained rollback as the incumbent. Returns the
    model restored, or None if no rollback is retained. Mutates ``registry``."""
    rc = get_role(registry, role)
    if not rc:
        return None
    rolls = rc.get("rollback") or []
    if not rolls:
        return None
    target = rolls.pop(0)
    prev = rc.get("model")
    rc["model"] = target
    # the deposed incumbent becomes the newest rollback so the swap is reversible
    if prev and prev != target:
        rolls.insert(0, prev)
    return target
