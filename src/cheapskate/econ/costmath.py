# SPDX-License-Identifier: Apache-2.0
"""Pure, deterministic cost math — THE quality bar of the repo.

Every function here is a pure function of its arguments: no I/O, no clock, no
config lookups. That is what lets the report be reproducible on a stranger's
machine and every formula be pinned to a hand-computed fixture.

Units, fixed once so the whole engine agrees:

  * power in WATTS, duration in SECONDS, energy in kWh, prices in USD.
  * cloud prices are USD per 1,000,000 tokens (per the pricing snapshot).

Formulas (as written equations):

  energy_kwh          = watts × duration_s / 3_600_000
                        (watts × seconds = joules; ÷3.6e6 J/kWh → kWh)
  energy_cost_usd     = energy_kwh × electricity_usd_per_kwh
  amortization_usd    = amortization_usd_per_month / tasks_in_month   (share)
  local_cost_usd      = energy_cost_usd + amortization_usd
                        (energy term omitted — treated as 0 contribution and
                         flagged — when watts or $/kWh is unknown)

  cloud_cost_usd      = tokens_in  / 1e6 × input_usd_per_mtok
                      + tokens_out / 1e6 × output_usd_per_mtok

  true multiplier     = total_attempts / successful_runs
                        where a run that retried locally AND escalated to cloud
                        is charged for BOTH the local attempts and the cloud call.
  true_local_cost     = Σ local attempt costs for a task type (incl. failed/retried
                        attempts) — the honesty nobody else models.
  true_cloud_cost     = Σ cloud call costs (incl. escalation calls).

  break_even_tokens_per_day (a→b, a cheaper fixed local, b per-token cloud):
                      = fixed_daily_local_usd / cloud_usd_per_token
                        i.e. the daily token volume above which the fixed local
                        cost is cheaper than paying per-token to the cloud.
"""

from __future__ import annotations

from dataclasses import dataclass

_JOULES_PER_KWH = 3_600_000.0  # 1 kWh = 3.6e6 J; watt-seconds are joules
_TOKENS_PER_MTOK = 1_000_000.0


# ── energy / local cost ──────────────────────────────────────────────────────


def energy_kwh(watts: float, duration_s: float) -> float:
    """Energy consumed by a task, in kWh. ``watts × seconds`` are joules; divide
    by 3.6e6 J/kWh. Negative inputs are clamped to 0 (no negative energy)."""
    if watts <= 0 or duration_s <= 0:
        return 0.0
    return (watts * duration_s) / _JOULES_PER_KWH


def energy_cost_usd(
    watts: float | None, duration_s: float, usd_per_kwh: float | None
) -> float | None:
    """Energy cost of a task in USD, or None when it cannot be known.

    Returns None (NOT 0) when watts or $/kWh is unknown — the "electricity
    unknown" mode. The caller must OMIT this from local cost, never treat an
    unknown as free."""
    if watts is None or usd_per_kwh is None:
        return None
    return energy_kwh(watts, duration_s) * usd_per_kwh


def amortization_share_usd(
    amortization_usd_per_month: float | None, tasks_in_month: int
) -> float | None:
    """The per-task share of a fixed monthly hardware cost. None when no
    amortization is configured; 0.0 when configured but no tasks ran."""
    if amortization_usd_per_month is None:
        return None
    if tasks_in_month <= 0:
        return 0.0
    return amortization_usd_per_month / tasks_in_month


def local_cost_usd(
    energy_usd: float | None, amortization_usd: float | None
) -> float:
    """Total local cost of one task: energy + amortization share. An unknown
    (None) component contributes 0 to the sum — but the caller is expected to
    report which components were unknown, so an unknown energy cost is visible
    as "energy: N/A", not silently swallowed as free."""
    return (energy_usd or 0.0) + (amortization_usd or 0.0)


# ── cloud cost ───────────────────────────────────────────────────────────────


def cloud_cost_usd(
    tokens_in: int,
    tokens_out: int,
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
) -> float:
    """Cost of one cloud call: input + output tokens priced per 1M tokens."""
    ti = max(0, tokens_in)
    to = max(0, tokens_out)
    return (ti / _TOKENS_PER_MTOK) * input_usd_per_mtok + (
        to / _TOKENS_PER_MTOK
    ) * output_usd_per_mtok


# ── true cost incl. retries + escalations (the differentiator) ───────────────


@dataclass(frozen=True)
class TrueCost:
    """The honest cost of a task type over a window.

    ``local_usd`` counts EVERY local attempt (first tries, retries, and the
    attempts that later escalated). ``cloud_usd`` counts every cloud call
    (escalations included). ``total_usd`` is their sum — a task that retried
    locally then escalated to the cloud is charged for both, which is the point.
    """

    runs: int  # successful task completions (the denominator for per-run figures)
    local_attempts: int  # total local model calls, incl. retries + pre-escalation
    cloud_calls: int  # total cloud calls, incl. escalations
    local_usd: float
    cloud_usd: float

    @property
    def total_usd(self) -> float:
        return self.local_usd + self.cloud_usd

    @property
    def retry_multiplier(self) -> float:
        """local_attempts / runs — >1.0 means retries inflated the true cost.
        Undefined (returns 1.0) when no successful runs to divide by."""
        if self.runs <= 0:
            return 1.0
        return self.local_attempts / self.runs

    @property
    def cost_per_run_usd(self) -> float:
        if self.runs <= 0:
            return 0.0
        return self.total_usd / self.runs


def true_cost(
    runs: int,
    local_attempts: int,
    cloud_calls: int,
    local_usd_per_attempt: float,
    cloud_usd_per_call: float,
) -> TrueCost:
    """Assemble a :class:`TrueCost` from counts and per-unit costs.

    ``local_attempts`` is the FULL attempt count (first try + every retry +
    every attempt that ultimately escalated). ``cloud_calls`` is the number of
    escalation/cloud calls. Both are charged — that is the true cost."""
    la = max(0, local_attempts)
    cc = max(0, cloud_calls)
    return TrueCost(
        runs=max(0, runs),
        local_attempts=la,
        cloud_calls=cc,
        local_usd=la * max(0.0, local_usd_per_attempt),
        cloud_usd=cc * max(0.0, cloud_usd_per_call),
    )


def cost_per_mtok(total_usd: float, total_tokens: int) -> float | None:
    """Effective $/1M tokens: total spend divided by tokens processed. None when
    no tokens (division undefined) rather than a misleading 0 or infinity."""
    if total_tokens <= 0:
        return None
    return total_usd / (total_tokens / _TOKENS_PER_MTOK)


# ── break-even ───────────────────────────────────────────────────────────────


def break_even_tokens_per_day(
    fixed_daily_local_usd: float, cloud_usd_per_token: float
) -> float | None:
    """Daily token volume at which a FIXED daily local cost equals paying the
    cloud per token. Above this volume local is cheaper; below it cloud is.

    = fixed_daily_local_usd / cloud_usd_per_token.

    None when the cloud per-token price is 0 (no break-even; cloud is free)."""
    if cloud_usd_per_token <= 0:
        return None
    return fixed_daily_local_usd / cloud_usd_per_token


def blended_cloud_usd_per_token(
    input_usd_per_mtok: float,
    output_usd_per_mtok: float,
    out_fraction: float = 0.5,
) -> float:
    """A single $/token for break-even math, blending input+output prices by the
    expected output fraction (default 50/50). ``out_fraction`` clamped to [0,1]."""
    f = min(1.0, max(0.0, out_fraction))
    per_mtok = (1 - f) * input_usd_per_mtok + f * output_usd_per_mtok
    return per_mtok / _TOKENS_PER_MTOK
