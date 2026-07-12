# SPDX-License-Identifier: Apache-2.0
"""Cost math: every formula pinned to a hand-computed fixture. Pure, deterministic.

These are THE quality bar of the repo — if any of these drift, the receipts lie.
"""

from __future__ import annotations

import math

from cheapskate.econ import costmath


def _close(a, b, tol=1e-9):
    return math.isclose(a, b, rel_tol=0, abs_tol=tol)


# ── energy ───────────────────────────────────────────────────────────────────


def test_energy_kwh_hand_computed():
    # 20 W for 3600 s = 72000 J = 0.02 kWh  (÷ 3.6e6 J/kWh)
    assert _close(costmath.energy_kwh(20.0, 3600.0), 0.02)


def test_energy_kwh_short_task():
    # 30 W for 12 s = 360 J = 0.0001 kWh
    assert _close(costmath.energy_kwh(30.0, 12.0), 0.0001)


def test_energy_kwh_nonpositive_is_zero():
    assert costmath.energy_kwh(0, 100) == 0.0
    assert costmath.energy_kwh(20, 0) == 0.0
    assert costmath.energy_kwh(-5, 100) == 0.0


def test_energy_cost_usd_hand_computed():
    # 0.02 kWh × $0.15/kWh = $0.003
    assert _close(costmath.energy_cost_usd(20.0, 3600.0, 0.15), 0.003)


def test_energy_cost_none_when_watts_unknown():
    assert costmath.energy_cost_usd(None, 3600.0, 0.15) is None


def test_energy_cost_none_when_price_unknown():
    assert costmath.energy_cost_usd(20.0, 3600.0, None) is None


# ── amortization ─────────────────────────────────────────────────────────────


def test_amortization_share_hand_computed():
    # $100/mo across 1000 tasks = $0.10/task
    assert _close(costmath.amortization_share_usd(100.0, 1000), 0.10)


def test_amortization_none_when_unconfigured():
    assert costmath.amortization_share_usd(None, 1000) is None


def test_amortization_zero_tasks_is_zero_not_error():
    assert costmath.amortization_share_usd(100.0, 0) == 0.0


# ── local cost combine ───────────────────────────────────────────────────────


def test_local_cost_sums_known_components():
    # energy 0.003 + amortization 0.10 = 0.103
    assert _close(costmath.local_cost_usd(0.003, 0.10), 0.103)


def test_local_cost_unknown_energy_contributes_zero():
    # energy unknown (None) → treated as 0 in the sum; amortization still counts
    assert _close(costmath.local_cost_usd(None, 0.10), 0.10)


def test_local_cost_all_unknown_is_zero():
    assert costmath.local_cost_usd(None, None) == 0.0


# ── cloud cost ───────────────────────────────────────────────────────────────


def test_cloud_cost_hand_computed():
    # 1000 in @ $2.5/Mtok = 0.0025 ; 500 out @ $10/Mtok = 0.005 ; total 0.0075
    assert _close(costmath.cloud_cost_usd(1000, 500, 2.5, 10.0), 0.0075)


def test_cloud_cost_zero_tokens_is_zero():
    assert costmath.cloud_cost_usd(0, 0, 2.5, 10.0) == 0.0


def test_cloud_cost_clamps_negative_tokens():
    assert costmath.cloud_cost_usd(-100, -50, 2.5, 10.0) == 0.0


# ── true cost incl. retries + escalations (the differentiator) ───────────────


def test_true_cost_charges_local_attempts_and_cloud_escalations():
    # 10 successful runs, but 15 total local attempts (5 retries) AND 3 escalated
    # to the cloud. Both are charged.
    tc = costmath.true_cost(
        runs=10,
        local_attempts=15,
        cloud_calls=3,
        local_usd_per_attempt=0.001,
        cloud_usd_per_call=0.02,
    )
    assert _close(tc.local_usd, 0.015)  # 15 × 0.001
    assert _close(tc.cloud_usd, 0.06)  # 3 × 0.02
    assert _close(tc.total_usd, 0.075)  # local + cloud, BOTH counted
    assert _close(tc.retry_multiplier, 1.5)  # 15 attempts / 10 runs
    assert _close(tc.cost_per_run_usd, 0.0075)  # 0.075 / 10


def test_true_cost_retry_multiplier_one_when_no_retries():
    tc = costmath.true_cost(10, 10, 0, 0.001, 0.02)
    assert _close(tc.retry_multiplier, 1.0)
    assert tc.cloud_usd == 0.0


def test_true_cost_zero_runs_is_safe():
    tc = costmath.true_cost(0, 0, 0, 0.001, 0.02)
    assert tc.retry_multiplier == 1.0
    assert tc.cost_per_run_usd == 0.0


def test_true_cost_clamps_negatives():
    tc = costmath.true_cost(-1, -5, -2, -0.5, -0.5)
    assert tc.local_usd == 0.0
    assert tc.cloud_usd == 0.0


# ── $/1M tokens ──────────────────────────────────────────────────────────────


def test_cost_per_mtok_hand_computed():
    # $0.075 over 500,000 tokens → $0.15 per 1M tokens
    assert _close(costmath.cost_per_mtok(0.075, 500_000), 0.15)


def test_cost_per_mtok_none_when_no_tokens():
    assert costmath.cost_per_mtok(0.075, 0) is None


# ── break-even ───────────────────────────────────────────────────────────────


def test_break_even_tokens_per_day_hand_computed():
    # fixed $1.00/day local ; cloud $0.00001/token → break-even at 100,000 tok/day
    assert _close(
        costmath.break_even_tokens_per_day(1.0, 0.00001), 100_000.0
    )


def test_break_even_none_when_cloud_free():
    assert costmath.break_even_tokens_per_day(1.0, 0.0) is None


def test_blended_cloud_usd_per_token_hand_computed():
    # 50/50 blend of $2.5 and $10 per Mtok = $6.25/Mtok = 6.25e-6/token
    per_tok = costmath.blended_cloud_usd_per_token(2.5, 10.0, out_fraction=0.5)
    assert _close(per_tok, 6.25e-6)


def test_blended_clamps_out_fraction():
    # out_fraction > 1 clamps to 1 → all output price ($10/Mtok = 1e-5/token)
    per_tok = costmath.blended_cloud_usd_per_token(2.5, 10.0, out_fraction=5.0)
    assert _close(per_tok, 1e-5)
