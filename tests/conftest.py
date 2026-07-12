# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures. Stubs the not-yet-written ``cheapskate.config`` and
``cheapskate.telemetry`` modules via sys.modules so this suite runs standalone."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point the XDG state/config dirs at a temp dir so tests never touch the
    real filesystem locations (key files, mlx state, locks)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    yield


@pytest.fixture
def stub_config_telemetry(monkeypatch):
    """Install stub ``cheapskate.config`` and ``cheapskate.telemetry`` modules.

    Returns the list of telemetry events recorded, so tests can assert on them.
    """
    events: list[tuple] = []

    cfg_mod = types.ModuleType("cheapskate.config")

    def load():
        return {
            "broker": {"host": "127.0.0.1", "port": 4747, "gate": "serial"},
            "machine": {"ram_budget_gb": 100},
            "roles": {"reasoning": {"model": "test-model", "backend": "ollama"}},
        }

    cfg_mod.load = load  # type: ignore[attr-defined]

    tel_mod = types.ModuleType("cheapskate.telemetry")

    def log_event(kind, **fields):
        events.append((kind, fields))

    tel_mod.log_event = log_event  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "cheapskate.config", cfg_mod)
    monkeypatch.setitem(sys.modules, "cheapskate.telemetry", tel_mod)
    return events
