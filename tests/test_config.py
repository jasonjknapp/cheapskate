# SPDX-License-Identifier: Apache-2.0
"""Config: shipped defaults, deep-merge of a user config.yaml, typed sections."""

from __future__ import annotations

import textwrap

from cheapskate import config as cfgmod


def test_defaults_shipped_as_data():
    cfg = cfgmod.Config()
    assert cfg.broker.port == 4747
    assert cfg.dial.default_level == 2
    assert cfg.dial.default_sub_dial == "std"
    # documented default fail-closed classes
    assert cfg.never_local == ["financial", "legal", "medical", "credentials"]
    assert cfg.never_cloud == []
    # generic shipped task types (no personal residue)
    assert set(cfg.task_types) == {"summarize", "draft", "classify", "extract", "review", "boilerplate"}
    # remote-URL backend entry present as the multi-machine story
    assert "remote" in cfg.backends
    assert cfg.backends["remote"].kind == "remote"
    # user profiles with quotas
    assert cfg.users["interactive"].key_class == "interactive"
    assert cfg.users["background"].quota.daily_requests is None


def test_machine_id_default_is_sanitized():
    cfg = cfgmod.Config()
    mid = cfg.machine.machine_id
    assert mid and all(c.isalnum() or c == "-" for c in mid)
    assert mid == mid.lower()


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = cfgmod.load(path=tmp_path / "nope.yaml")
    assert cfg.broker.port == 4747


def test_user_config_deep_merges_over_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(
            """
            broker:
              port: 5000
            never_cloud:
              - internal-secrets
            task_types:
              summarize:
                min_level: 2
            """
        )
    )
    cfg = cfgmod.load(path=p)
    # overridden
    assert cfg.broker.port == 5000
    # sibling default preserved through the deep-merge
    assert cfg.broker.host == "127.0.0.1"
    # new never_cloud entry applied
    assert cfg.never_cloud == ["internal-secrets"]
    # nested task-type key merged, other task types still present
    assert cfg.task_types["summarize"].min_level == 2
    assert cfg.task_types["summarize"].role == "reasoning"  # untouched default
    assert "classify" in cfg.task_types


def test_non_mapping_config_rejected(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n")
    try:
        cfgmod.load(path=p)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a non-mapping config")
