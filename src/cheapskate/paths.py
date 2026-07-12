# SPDX-License-Identifier: Apache-2.0
"""XDG path helpers — the only module that derives filesystem locations."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg(env_var: str, default: str) -> Path:
    root = os.environ.get(env_var, "").strip()
    base = Path(root) if root else Path.home() / default
    return base / "cheapskate"


def config_dir() -> Path:
    d = _xdg("XDG_CONFIG_HOME", ".config")
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    d = _xdg("XDG_STATE_HOME", ".local/state")
    d.mkdir(parents=True, exist_ok=True)
    return d
