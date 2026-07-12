# SPDX-License-Identifier: Apache-2.0
"""Registry: roles → model/backend/fallback/rollback/quarantine (atomic writes),
and the guarded model-currency engine (discover → evaluate → promote/rollback →
prune) that never touches an incumbent, a fallback, or a retained rollback.
"""

from __future__ import annotations

from . import currency, registry
from .currency import (
    candidate_fits,
    candidate_size_gb,
    discover,
    evaluate,
    free_disk_gb,
    prune_candidates,
    same_lineage,
)
from .registry import (
    KEEP_ROLLBACK_N,
    get_role,
    incumbent,
    load,
    protected_models,
    quarantine,
    save,
    set_incumbent,
)

# promote/rollback exist in both modules with different signatures; expose the
# currency-engine versions (they orchestrate the registry ones) by module.
__all__ = [
    "registry",
    "currency",
    "load",
    "save",
    "get_role",
    "incumbent",
    "set_incumbent",
    "protected_models",
    "quarantine",
    "KEEP_ROLLBACK_N",
    "discover",
    "evaluate",
    "candidate_fits",
    "candidate_size_gb",
    "free_disk_gb",
    "prune_candidates",
    "same_lineage",
]
