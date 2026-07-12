# SPDX-License-Identifier: Apache-2.0
"""Backend layer: resolve a role/model to a serving engine, and manage the
single-large-model lifecycle (MLX server + Ollama/LM Studio residency).

The load-bearing safety semantics live here:
  * a machine-wide flock serializes every large-model load/swap,
  * co-resident models in other runtimes are de-loaded before a large load,
  * a model whose footprint exceeds the RAM budget is refused (fail-closed).
"""

from __future__ import annotations

from .resolve import BackendSpec, LocalUnavailable, default_endpoint, resolve
from .mlx import ensure_mlx, stop_mlx
from .ollama import (
    lms_loaded,
    lms_resident_gb,
    lms_unload_all,
    ollama_resident_gb,
    ollama_stop_all,
)
from .preflight import ensure_role, evict_coresidents, release, resident_check

__all__ = [
    "BackendSpec",
    "LocalUnavailable",
    "default_endpoint",
    "resolve",
    "ensure_mlx",
    "stop_mlx",
    "lms_loaded",
    "lms_resident_gb",
    "lms_unload_all",
    "ollama_resident_gb",
    "ollama_stop_all",
    "evict_coresidents",
    "ensure_role",
    "release",
    "resident_check",
]
