# SPDX-License-Identifier: Apache-2.0
"""Dial: level 0..3, sub-dial only at level 2, fresh read of the state file
(never cached), missing file ⇒ config default."""

from __future__ import annotations

from cheapskate.config import Config
from cheapskate.router import dial as d


def test_parse_basic():
    assert d.parse_dial("2:max") == (2, "max")
    assert d.parse_dial("1") == (1, None)
    assert d.parse_dial("3") == (3, None)
    assert d.parse_dial("0") == (0, None)


def test_parse_label_prefix():
    assert d.parse_dial("level-2:std") == (2, "std")


def test_sub_dial_only_meaningful_at_level_2():
    # a sub-dial at a non-2 level is dropped
    assert d.parse_dial("1:max") == (1, None)
    assert d.parse_dial("3:lite") == (3, None)
    # at level 2 an invalid/absent sub-dial normalizes to std
    assert d.parse_dial("2") == (2, "std")
    assert d.parse_dial("2:bogus") == (2, "std")


def test_junk_and_out_of_range_return_default():
    assert d.parse_dial("garbage", 2, "std") == (2, "std")
    assert d.parse_dial("", 1, "std") == (1, None)
    assert d.parse_dial("9", 2, "std") == (2, "std")  # out of range
    assert d.parse_dial("-4", 2, "std") == (2, "std")


def test_read_missing_file_uses_config_default(tmp_path):
    cfg = Config()  # default_level=2, default_sub_dial=std
    assert d.read_dial(cfg, path=tmp_path / "dial") == (2, "std")


def test_read_is_fresh_not_cached(tmp_path):
    cfg = Config()
    state = tmp_path / "dial"
    state.write_text("1\n")
    assert d.read_dial(cfg, path=state) == (1, None)
    # rewrite the file — the very next read must see it (no caching)
    state.write_text("2:max\n")
    assert d.read_dial(cfg, path=state) == (2, "max")


def test_write_round_trips_canonically(tmp_path):
    cfg = Config()
    state = tmp_path / "dial"
    assert d.write_dial(cfg, 2, "lite", path=state) == (2, "lite")
    assert state.read_text().strip() == "2:lite"
    assert d.read_dial(cfg, path=state) == (2, "lite")
    # a non-2 level drops the sub-dial on write
    d.write_dial(cfg, 3, "max", path=state)
    assert state.read_text().strip() == "3"


def test_format():
    assert d.format_dial(2, "max") == "2:max"
    assert d.format_dial(1, None) == "1"
    assert d.format_dial(2, None) == "2"
