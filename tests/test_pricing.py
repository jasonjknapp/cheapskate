# SPDX-License-Identifier: Apache-2.0
"""Pricing: bundled snapshot loads, staleness warns (never fails), exact +
fuzzy lookup with fuzzy clearly flagged, user override wins and degrades safely."""

from __future__ import annotations

import json
from datetime import date

import pytest

from cheapskate.econ import pricing


def test_bundled_snapshot_loads_and_is_nonempty():
    snap = pricing.load_pricing()
    assert snap.origin == "bundled"
    assert len(snap.rows) >= 8  # the seed carries ~8-12 popular models
    # every row carries source + as_of provenance
    for row in snap.rows.values():
        assert row.source
        assert row.as_of
        assert row.input_usd_per_mtok >= 0
        assert row.output_usd_per_mtok >= 0


def test_seed_has_expected_reference_models():
    snap = pricing.load_pricing()
    # a couple of stable, popular anchors the report defaults to
    assert "gpt-4o-mini" in snap.rows
    assert "gpt-4o" in snap.rows


def test_exact_lookup_is_not_fuzzy():
    snap = pricing.load_pricing()
    row = pricing.lookup(snap, "gpt-4o-mini")
    assert row is not None
    assert row.fuzzy is False
    assert row.id == "gpt-4o-mini"


def test_fuzzy_prefix_match_is_flagged():
    snap = pricing.load_pricing()
    # a dated variant that is not an exact id → fuzzy prefix match to gpt-4o
    row = pricing.lookup(snap, "gpt-4o-2026-05-01")
    assert row is not None
    assert row.fuzzy is True
    assert row.matched_id is not None
    # it resolved to a real seed row
    assert row.id in snap.rows


def test_unknown_model_returns_none():
    snap = pricing.load_pricing()
    assert pricing.lookup(snap, "totally-made-up-model-xyz") is None
    assert pricing.lookup(snap, "") is None


def test_fuzzy_prefers_longest_match_deterministically():
    rows = {
        "gpt-4o": pricing.PriceRow("gpt-4o", 2.5, 10.0, "s", "2026-07-01"),
        "gpt-4o-mini": pricing.PriceRow("gpt-4o-mini", 0.15, 0.6, "s", "2026-07-01"),
    }
    snap = pricing.PricingSnapshot(rows=rows, origin="bundled", path=pricing._BUNDLED)
    # query is a prefix of both; the longest matched id wins
    row = pricing.lookup(snap, "gpt-4o-mini-2026")
    assert row is not None
    assert row.id == "gpt-4o-mini"
    assert row.fuzzy is True


def test_staleness_warns_when_old_but_does_not_raise():
    snap = pricing.load_pricing()
    # far-future reference date forces staleness
    future = date(2099, 1, 1)
    warning = pricing.staleness_warning(snap, max_age_days=14, today=future)
    assert warning is not None
    assert "stale" in warning.lower() or "old" in warning.lower()


def test_staleness_none_when_fresh():
    rows = {"m": pricing.PriceRow("m", 1.0, 2.0, "s", "2026-07-10")}
    snap = pricing.PricingSnapshot(rows=rows, origin="bundled", path=pricing._BUNDLED)
    assert pricing.staleness_warning(snap, 14, today=date(2026, 7, 12)) is None


def test_staleness_days_computes():
    rows = {"m": pricing.PriceRow("m", 1.0, 2.0, "s", "2026-07-01")}
    snap = pricing.PricingSnapshot(rows=rows, origin="bundled", path=pricing._BUNDLED)
    assert pricing.staleness_days(snap, today=date(2026, 7, 15)) == 14


def test_override_wins_when_present(tmp_path):
    override_dir = tmp_path
    (override_dir / "pricing.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "custom-model",
                        "input_usd_per_mtok": 9.0,
                        "output_usd_per_mtok": 18.0,
                        "source": "my-vendor",
                        "as_of": "2026-07-11",
                    }
                ]
            }
        )
    )
    snap = pricing.load_pricing(config_dir=override_dir)
    assert snap.origin == "override"
    assert "custom-model" in snap.rows
    assert snap.rows["custom-model"].input_usd_per_mtok == 9.0


def test_bad_override_falls_back_to_bundled(tmp_path):
    (tmp_path / "pricing.json").write_text("{ this is not json")
    snap = pricing.load_pricing(config_dir=tmp_path)
    assert snap.origin == "bundled"
    assert len(snap.rows) >= 8


def test_malformed_row_is_skipped_not_fatal(tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(
            {
                "models": [
                    {"id": "good", "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0},
                    {"id": "bad-missing-price"},
                ]
            }
        )
    )
    snap = pricing.load_pricing(config_dir=tmp_path)
    assert "good" in snap.rows
    assert "bad-missing-price" not in snap.rows


@pytest.mark.parametrize("bad_date", ["", "not-a-date", "2026-13-40"])
def test_undatable_snapshot_warns_age_unknown(bad_date):
    rows = {"m": pricing.PriceRow("m", 1.0, 2.0, "s", bad_date)}
    snap = pricing.PricingSnapshot(rows=rows, origin="bundled", path=pricing._BUNDLED)
    assert snap.newest_as_of() is None
    warning = pricing.staleness_warning(snap, 14)
    assert warning is not None and "unknown" in warning.lower()
