# SPDX-License-Identifier: Apache-2.0
"""Integration-seam pins: contracts BETWEEN the packages (client↔task,
config↔broker, registry↔backends) that no single package's suite covers."""

import importlib

from cheapskate import client as client_mod
from cheapskate.broker.app import _budget_gb
from cheapskate.registry import registry as registry_mod
from cheapskate.router.task import (
    _completion_text,
    _completion_tokens,
    _default_complete,
)

# The backends package re-exports the resolve FUNCTION, shadowing the submodule
# on attribute access — import the module explicitly.
resolve_mod = importlib.import_module("cheapskate.backends.resolve")


def test_default_complete_preserves_rich_dict_for_token_capture(monkeypatch):
    """client.complete returns a rich dict; the task layer's default path now
    preserves it (so token counts reach the econ receipt), and the normalizers
    extract text and tokens from it."""
    monkeypatch.setattr(
        client_mod, "complete",
        lambda prompt, **kw: {"text": "adapted!", "model": "m",
                              "prompt_eval_count": 12, "eval_count": 7},
    )
    fn = _default_complete()
    raw = fn("hi", system="s", role="reasoning")
    assert _completion_text(raw) == "adapted!"
    assert _completion_tokens(raw) == (12, 7)
    # A bare-string completion (the historical injected-fn contract) still works.
    assert _completion_text("plain") == "plain"
    assert _completion_tokens("plain") == (None, None)


def test_roles_fall_back_to_registry_yaml(tmp_path, monkeypatch):
    """With no roles on the config, backends resolve from registry.yaml."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    registry_mod.save({"roles": {"code": {"model": "m1", "backend": "ollama"}}})
    roles = resolve_mod._roles(config={})
    assert roles["code"]["model"] == "m1"


def test_roles_on_config_win_over_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    registry_mod.save({"roles": {"code": {"model": "from-registry"}}})
    roles = resolve_mod._roles(config={"roles": {"code": {"model": "from-config"}}})
    assert roles["code"]["model"] == "from-config"


def test_budget_subtracts_headroom_from_detected_ram():
    cfg = {"machine": {"ram_gb": 64.0, "ram_headroom_gb": 24.0}}
    assert _budget_gb(cfg) == 40.0


def test_budget_explicit_override_wins():
    cfg = {"machine": {"ram_gb": 64.0, "ram_budget_gb": 10.0}}
    assert _budget_gb(cfg) == 10.0


def test_budget_unknown_ram_fails_closed():
    assert _budget_gb({"machine": {"ram_gb": None}}) == 0.0
    assert _budget_gb({"machine": {}}) == 0.0
