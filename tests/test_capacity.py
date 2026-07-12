# SPDX-License-Identifier: Apache-2.0
"""Pins the pure capacity decision. No network, no processes."""

from __future__ import annotations

from cheapskate.broker.capacity import capacity_decision


def test_resident_model_is_ok():
    action, _ = capacity_decision(40, 0, False, 100, model_resident=True)
    assert action == "ok"


def test_unsized_model_is_ok():
    action, _ = capacity_decision(0, 0, False, 100)
    assert action == "ok"


def test_model_larger_than_budget_refuses_fail_closed():
    action, reason = capacity_decision(120, 0, False, 100)
    assert action == "503"
    assert "exceeds" in reason.lower()


def test_fits_within_budget_ok():
    action, _ = capacity_decision(40, 20, False, 100)
    assert action == "ok"


def test_over_soft_budget_ollama_self_evicts():
    # No secondary runtime loaded; projected > budget → ollama LRU-evicts itself.
    action, _ = capacity_decision(60, 50, False, 100)
    assert action == "ok-selfevict"


def test_secondary_runtime_coexists_when_it_fits():
    # projected (30+10) + secondary 20 = 60 <= 100 → coexist, no eviction.
    action, _ = capacity_decision(30, 10, True, 100, lms_gb=20)
    assert action == "ok"


def test_secondary_runtime_evicted_under_pressure():
    # projected (60+30) + secondary 40 = 130 > 100 → evict the secondary runtime.
    action, _ = capacity_decision(60, 30, True, 100, lms_gb=40)
    assert action == "evict-lms"


def test_unknown_secondary_footprint_conservatively_evicts():
    # lms_gb=None (loaded but unsized) → conservative preempt (safety > politeness).
    action, _ = capacity_decision(30, 10, True, 100, lms_gb=None)
    assert action == "evict-lms"


def test_budget_boundary_is_inclusive():
    # Exactly at budget must NOT refuse.
    action, _ = capacity_decision(100, 0, False, 100)
    assert action == "ok"
