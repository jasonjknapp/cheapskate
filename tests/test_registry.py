# SPDX-License-Identifier: Apache-2.0
"""Registry: atomic load/save, incumbent swap with rollback retention, the
protected set (incumbent + fallback + rollback), quarantine."""

from __future__ import annotations

import yaml

from cheapskate.registry import registry as reg


def test_load_missing_returns_empty(tmp_path):
    r = reg.load(path=tmp_path / "registry.yaml")
    assert r == {"roles": {}}


def test_set_incumbent_and_atomic_save(tmp_path):
    p = tmp_path / "registry.yaml"
    r = reg.load(path=p)
    reg.set_incumbent(r, "reasoning", "family/model-a", "mlx", approx_gb=65.0, fallback="family/fb")
    reg.save(r, path=p)
    # atomic write left no temp file behind
    assert not (tmp_path / "registry.yaml.tmp").exists()
    loaded = yaml.safe_load(p.read_text())
    assert loaded["roles"]["reasoning"]["model"] == "family/model-a"
    assert loaded["roles"]["reasoning"]["backend"] == "mlx"


def test_swap_retains_previous_as_rollback():
    r = {"roles": {}}
    reg.set_incumbent(r, "code", "vendor/old", "ollama")
    reg.set_incumbent(r, "code", "vendor/new", "ollama")
    rc = r["roles"]["code"]
    assert rc["model"] == "vendor/new"
    assert rc["rollback"] == ["vendor/old"]


def test_rollback_retention_bounded():
    r = {"roles": {}}
    reg.set_incumbent(r, "code", "m1", "ollama")
    reg.set_incumbent(r, "code", "m2", "ollama")
    reg.set_incumbent(r, "code", "m3", "ollama")
    # keep_n default 1 → only the most recent prior retained
    assert r["roles"]["code"]["rollback"] == ["m2"]


def test_protected_set_includes_incumbent_fallback_rollback():
    r = {"roles": {}}
    reg.set_incumbent(r, "reasoning", "inc", "mlx", fallback="fb")
    reg.set_incumbent(r, "reasoning", "inc2", "mlx", fallback="fb")  # inc → rollback
    protected = reg.protected_models(r)
    assert {"inc2", "fb", "inc"} <= protected


def test_rollback_restores_previous():
    r = {"roles": {}}
    reg.set_incumbent(r, "code", "old", "ollama")
    reg.set_incumbent(r, "code", "new", "ollama")
    restored = reg.rollback(r, "code")
    assert restored == "old"
    assert r["roles"]["code"]["model"] == "old"
    # reversible: 'new' is now the rollback
    assert r["roles"]["code"]["rollback"] == ["new"]


def test_rollback_none_when_no_history():
    r = {"roles": {"x": {"model": "m", "backend": "ollama"}}}
    assert reg.rollback(r, "x") is None


def test_quarantine():
    r = {"roles": {}}
    reg.set_incumbent(r, "code", "m", "ollama")
    reg.quarantine(r, "code", "bad/model")
    assert reg.is_quarantined(r, "code", "bad/model")
    assert not reg.is_quarantined(r, "code", "m")
