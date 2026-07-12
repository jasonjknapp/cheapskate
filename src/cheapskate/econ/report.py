# SPDX-License-Identifier: Apache-2.0
"""Receipts from telemetry: read ``state_dir()/telemetry.jsonl``, group the
generation events by task type, and produce the per-task-type table, the
monthly receipts summary, and the content-free ``--share`` aggregate.

Input schema (the content-free feed the telemetry writer emits): one JSON line
per event. We consume ``kind == "generation"`` events with fields
``model, backend, machine_id, task_type, user, route, duration_s, tokens_in,
tokens_out, retries, escalated, ok``. (``kind == "task.run"`` from the router is
also accepted as a generation event so the report works before a dedicated
generation emitter exists.)

HARD RULE — ``--share`` is content-free BY CONSTRUCTION. It emits ONLY aggregate
numbers, model names, and ``machine_id``. It never reads or emits any free-text
field. A poisoned telemetry line whose text fields carry secrets can never
surface in ``--share`` output — pinned by a test.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .. import paths
from ..config import Config
from . import costmath, pricing
from .power import PowerReading, read_power

# Event kinds we treat as a "generation" (a routed model call worth costing).
_GENERATION_KINDS = frozenset({"generation", "task.run"})

# The ONLY fields --share is ever allowed to read off a raw event. Anything
# outside this allowlist (i.e. any free-text field) is structurally unreachable
# from the share path. This is the content-free guarantee, as data.
_SHARE_SAFE_EVENT_FIELDS = frozenset(
    {"kind", "task_type", "route", "model", "machine_id", "retries", "escalated", "ok"}
)


@dataclass
class TaskTypeStats:
    """Accumulated, content-free counts for one task type."""

    task_type: str
    runs: int = 0  # events (attempts to complete this task type)
    ok_runs: int = 0
    local_runs: int = 0
    cloud_runs: int = 0
    total_retries: int = 0
    escalations: int = 0
    total_duration_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    models: set[str] = field(default_factory=set)

    @property
    def pct_local(self) -> float:
        return (self.local_runs / self.runs) if self.runs else 0.0

    @property
    def retry_rate(self) -> float:
        return (self.total_retries / self.runs) if self.runs else 0.0

    @property
    def escalation_rate(self) -> float:
        return (self.escalations / self.runs) if self.runs else 0.0

    @property
    def quality_pass_rate(self) -> float:
        return (self.ok_runs / self.runs) if self.runs else 0.0

    @property
    def tokens_total(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def tokens_per_sec(self) -> float | None:
        if self.total_duration_s <= 0 or self.tokens_out <= 0:
            return None
        return self.tokens_out / self.total_duration_s


# ── reading telemetry ────────────────────────────────────────────────────────


def _telemetry_path() -> Path:
    return paths.state_dir() / "telemetry.jsonl"


def iter_events(path: Path | None = None) -> Iterable[dict[str, Any]]:
    """Yield parsed telemetry events. Missing file ⇒ nothing. A malformed line is
    skipped, never fatal."""
    p = path if path is not None else _telemetry_path()
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _event_month(evt: dict[str, Any]) -> str | None:
    ts = evt.get("ts")
    if not isinstance(ts, str):
        return None
    return ts[:7] if len(ts) >= 7 else None  # "YYYY-MM"


def _is_generation(evt: dict[str, Any]) -> bool:
    return evt.get("kind") in _GENERATION_KINDS


def collect_stats(
    events: Iterable[dict[str, Any]], *, month: str | None = None
) -> dict[str, TaskTypeStats]:
    """Fold generation events into per-task-type stats. ``month`` (``YYYY-MM``)
    filters by the event ``ts`` when given."""
    stats: dict[str, TaskTypeStats] = defaultdict(lambda: TaskTypeStats(task_type=""))
    for evt in events:
        if not _is_generation(evt):
            continue
        if month is not None and _event_month(evt) != month:
            continue
        tt = str(evt.get("task_type") or "unknown")
        st = stats[tt]
        st.task_type = tt
        st.runs += 1
        if evt.get("ok"):
            st.ok_runs += 1
        route = evt.get("route")
        if route == "local":
            st.local_runs += 1
        elif route == "cloud":
            st.cloud_runs += 1
        st.total_retries += int(evt.get("retries") or 0)
        if evt.get("escalated"):
            st.escalations += 1
        st.total_duration_s += float(evt.get("duration_s") or 0.0)
        st.tokens_in += int(evt.get("tokens_in") or 0)
        st.tokens_out += int(evt.get("tokens_out") or 0)
        model = evt.get("model")
        if isinstance(model, str) and model:
            st.models.add(model)
    return dict(stats)


# ── costing a task type ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskTypeReport:
    """The per-task-type row plus its derived recommendation."""

    stats: TaskTypeStats
    local_cost_per_run_usd: float | None  # None ⇒ energy unknown & no amort
    cloud_equiv_per_run_usd: float | None  # what an all-cloud run would cost
    local_usd_per_mtok: float | None
    cloud_usd_per_mtok: float | None
    cloud_model_id: str | None
    cloud_fuzzy: bool
    recommendation: str  # stay-local | go-cloud | mixed | insufficient-data
    energy_known: bool


def _pick_cloud_reference(
    snapshot: pricing.PricingSnapshot, cloud_ref_model: str
) -> pricing.PriceRow | None:
    return pricing.lookup(snapshot, cloud_ref_model)


def _recommend(
    st: TaskTypeStats,
    local_per_run: float | None,
    cloud_per_run: float | None,
) -> str:
    """A conservative recommendation. Insufficient data below a floor of runs.
    Otherwise: cheaper side wins with a margin; a high escalation rate nudges to
    mixed (local drafts, cloud rescues)."""
    if st.runs < 5:
        return "insufficient-data"
    # escalation-heavy work is inherently mixed regardless of unit cost
    if st.escalation_rate >= 0.30:
        return "mixed"
    if local_per_run is None or cloud_per_run is None:
        # no comparable cost basis — lean on where the work already runs
        if st.pct_local >= 0.8:
            return "stay-local"
        if st.pct_local <= 0.2:
            return "go-cloud"
        return "mixed"
    if local_per_run < cloud_per_run * 0.9:
        return "stay-local"
    if local_per_run > cloud_per_run * 1.1:
        return "go-cloud"
    return "mixed"


def build_task_reports(
    stats: dict[str, TaskTypeStats],
    config: Config,
    snapshot: pricing.PricingSnapshot,
    power: PowerReading,
    *,
    cloud_ref_model: str = "gpt-4o-mini",
    tasks_in_month: int | None = None,
) -> list[TaskTypeReport]:
    """Cost each task type. ``cloud_ref_model`` is the yardstick a local run is
    compared against ("what would this have cost all-cloud"). Pure w.r.t. its
    inputs — power + pricing + config are passed in, not fetched here."""
    econ = config.econ
    ref_row = _pick_cloud_reference(snapshot, cloud_ref_model)
    total_runs = sum(s.runs for s in stats.values()) or 0
    tim = tasks_in_month if tasks_in_month is not None else total_runs

    reports: list[TaskTypeReport] = []
    for tt, st in sorted(stats.items()):
        avg_duration = (st.total_duration_s / st.runs) if st.runs else 0.0

        energy_usd = costmath.energy_cost_usd(
            power.watts, avg_duration, econ.electricity_usd_per_kwh
        )
        amort_usd = costmath.amortization_share_usd(
            econ.hardware_amortization_usd_per_month, tim
        )
        energy_known = energy_usd is not None
        local_per_run: float | None
        if energy_usd is None and amort_usd is None:
            local_per_run = None  # nothing knowable about local cost
        else:
            local_per_run = costmath.local_cost_usd(energy_usd, amort_usd)

        # cloud-equivalent per run: price this task type's average token I/O at
        # the reference cloud model.
        cloud_per_run: float | None = None
        cloud_mtok: float | None = None
        if ref_row is not None and st.runs:
            avg_in = st.tokens_in / st.runs
            avg_out = st.tokens_out / st.runs
            cloud_per_run = costmath.cloud_cost_usd(
                int(round(avg_in)),
                int(round(avg_out)),
                ref_row.input_usd_per_mtok,
                ref_row.output_usd_per_mtok,
            )
            cloud_mtok = costmath.cost_per_mtok(
                cloud_per_run * st.runs, st.tokens_total
            )

        local_mtok = (
            costmath.cost_per_mtok(local_per_run * st.runs, st.tokens_total)
            if (local_per_run is not None and st.runs)
            else None
        )

        reports.append(
            TaskTypeReport(
                stats=st,
                local_cost_per_run_usd=local_per_run,
                cloud_equiv_per_run_usd=cloud_per_run,
                local_usd_per_mtok=local_mtok,
                cloud_usd_per_mtok=cloud_mtok,
                cloud_model_id=(ref_row.id if ref_row else None),
                cloud_fuzzy=(ref_row.fuzzy if ref_row else False),
                recommendation=_recommend(st, local_per_run, cloud_per_run),
                energy_known=energy_known,
            )
        )
    return reports


# ── monthly receipts ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Receipts:
    """The month's headline savings receipt (all aggregate, content-free)."""

    month: str
    total_runs: int
    pct_local: float
    quality_pass_rate: float
    cloud_spend_usd: float  # what actually went to the cloud (escalations + cloud routes)
    all_cloud_usd: float  # what every run would have cost all-cloud (reference)
    saved_usd: float
    assumptions: list[str]


def compute_receipts(
    reports: list[TaskTypeReport],
    *,
    month: str,
    assumptions: list[str],
) -> Receipts:
    """Roll per-task-type reports into the month's savings receipt.

    ``all_cloud_usd`` = every run priced at the cloud reference. ``cloud_spend_usd``
    = only the runs that actually hit the cloud (cloud-routed + escalated) priced
    the same way. ``saved`` = the difference (never negative)."""
    total_runs = sum(r.stats.runs for r in reports)
    local_runs = sum(r.stats.local_runs for r in reports)
    ok_runs = sum(r.stats.ok_runs for r in reports)

    all_cloud = 0.0
    actual_cloud = 0.0
    for r in reports:
        per_run = r.cloud_equiv_per_run_usd
        if per_run is None:
            continue
        all_cloud += per_run * r.stats.runs
        # runs that already went cloud, plus escalations off local runs
        cloud_hitting = r.stats.cloud_runs + r.stats.escalations
        actual_cloud += per_run * cloud_hitting

    saved = max(0.0, all_cloud - actual_cloud)
    return Receipts(
        month=month,
        total_runs=total_runs,
        pct_local=(local_runs / total_runs) if total_runs else 0.0,
        quality_pass_rate=(ok_runs / total_runs) if total_runs else 0.0,
        cloud_spend_usd=round(actual_cloud, 4),
        all_cloud_usd=round(all_cloud, 4),
        saved_usd=round(saved, 4),
        assumptions=assumptions,
    )


# ── rendering (plain text, no color deps) ────────────────────────────────────


def _fmt_usd(v: float | None) -> str:
    return "N/A" if v is None else f"${v:,.4f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def _fmt_num(v: float | None, nd: int = 1) -> str:
    return "N/A" if v is None else f"{v:.{nd}f}"


def render_report(
    reports: list[TaskTypeReport],
    receipts: Receipts,
    *,
    pricing_origin: str,
    staleness: str | None,
    power: PowerReading,
) -> str:
    """The human-readable ``report`` output: per-task-type table + receipts.
    Plain text, fixed-width columns, no color."""
    lines: list[str] = []
    lines.append(f"Cheapskate report — {receipts.month}")
    lines.append("=" * 60)
    lines.append(
        f"pricing: {pricing_origin}"
        + (f"  [WARN: {staleness}]" if staleness else "")
    )
    lines.append(f"power: {power.mode} ({power.detail})")
    lines.append("")

    cols = (
        ("task_type", 14),
        ("runs", 6),
        ("%local", 7),
        ("retry", 7),
        ("escal", 7),
        ("tok/s", 8),
        ("local$/Mtok", 12),
        ("cloud$/Mtok", 12),
        ("recommend", 16),
    )
    header = "".join(name.ljust(w) for name, w in cols)
    lines.append(header)
    lines.append("-" * len(header))
    for r in reports:
        s = r.stats
        row = "".join(
            v.ljust(w)
            for v, (_, w) in zip(
                [
                    s.task_type[:13],
                    str(s.runs),
                    _fmt_pct(s.pct_local),
                    _fmt_pct(s.retry_rate),
                    _fmt_pct(s.escalation_rate),
                    _fmt_num(s.tokens_per_sec),
                    _fmt_usd(r.local_usd_per_mtok),
                    _fmt_usd(r.cloud_usd_per_mtok),
                    r.recommendation,
                ],
                cols,
                strict=False,
            )
        )
        lines.append(row)

    lines.append("")
    lines.append("Receipts")
    lines.append("-" * 60)
    lines.append(
        f"routed {_fmt_pct(receipts.pct_local)} local across {receipts.total_runs} runs, "
        f"saved {_fmt_usd(receipts.saved_usd)} vs all-cloud "
        f"(all-cloud {_fmt_usd(receipts.all_cloud_usd)}, actual cloud "
        f"{_fmt_usd(receipts.cloud_spend_usd)}), quality pass "
        f"{_fmt_pct(receipts.quality_pass_rate)}"
    )
    if receipts.assumptions:
        lines.append("assumptions:")
        for a in receipts.assumptions:
            lines.append(f"  - {a}")
    return "\n".join(lines)


# ── --share : content-free aggregate markdown ────────────────────────────────


def render_share(
    reports: list[TaskTypeReport],
    receipts: Receipts,
    *,
    machine_id: str,
) -> str:
    """Emit an aggregate-only markdown savings receipt safe to post publicly.

    CONTENT-FREE BY CONSTRUCTION: this function reads ONLY numeric aggregates,
    model names (from the pricing reference / stats.models), and ``machine_id``.
    It NEVER reads a free-text telemetry field. Even if a poisoned telemetry line
    stuffed a secret into a text field, that field is never touched on this path,
    so it cannot appear here. Pinned by test_report.

    Note: model names come from the pricing snapshot's reference id and the
    aggregated ``stats.models`` set (model identifiers, not content)."""
    lines: list[str] = []
    lines.append(f"## Cheapskate savings — {receipts.month}")
    lines.append("")
    lines.append(f"- machine: `{_scrub_ident(machine_id)}`")
    lines.append(f"- runs: **{receipts.total_runs}**")
    lines.append(f"- routed local: **{_fmt_pct(receipts.pct_local)}**")
    lines.append(f"- quality pass: **{_fmt_pct(receipts.quality_pass_rate)}**")
    lines.append(
        f"- saved vs all-cloud: **{_fmt_usd(receipts.saved_usd)}** "
        f"(all-cloud {_fmt_usd(receipts.all_cloud_usd)})"
    )
    lines.append("")
    lines.append("| task type | runs | % local | retry | escalation | recommend |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for r in reports:
        s = r.stats
        lines.append(
            f"| {_scrub_ident(s.task_type)} | {s.runs} | {_fmt_pct(s.pct_local)} | "
            f"{_fmt_pct(s.retry_rate)} | {_fmt_pct(s.escalation_rate)} | {r.recommendation} |"
        )
    models = sorted({m for r in reports for m in r.stats.models})
    if models:
        lines.append("")
        lines.append("models: " + ", ".join(f"`{_scrub_ident(m)}`" for m in models))
    if receipts.assumptions:
        lines.append("")
        lines.append("_assumptions: " + "; ".join(receipts.assumptions) + "_")
    return "\n".join(lines)


def _scrub_ident(value: str) -> str:
    """Defense in depth: even identifiers (task_type, model, machine_id) get any
    markdown/newline control characters stripped so a hostile identifier can't
    inject formatting into the shared receipt. Not a content path — identifiers
    only — but belt-and-suspenders for the public-output surface."""
    return "".join(c for c in str(value) if c.isprintable() and c not in "|`\n\r").strip()


# ── top-level orchestration (the CLI entry) ──────────────────────────────────


@dataclass(frozen=True)
class ReportBundle:
    reports: list[TaskTypeReport]
    receipts: Receipts
    pricing_origin: str
    staleness: str | None
    power: PowerReading


def _default_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def generate(
    config: Config,
    *,
    month: str | None = None,
    path: Path | None = None,
    cloud_ref_model: str = "gpt-4o-mini",
    today: date | None = None,
) -> ReportBundle:
    """Read telemetry → stats → costed reports → receipts. No network; power is
    resolved in no-measure mode (config estimate or unknown) so the CLI never
    triggers a privileged probe by default."""
    m = month or _default_month()
    snapshot = pricing.load_pricing()
    staleness = pricing.staleness_warning(
        snapshot, config.econ.pricing_max_age_days, today=today
    )
    power = read_power(watts_estimate=config.econ.watts_estimate)  # no measurement

    stats = collect_stats(iter_events(path), month=m)
    reports = build_task_reports(
        stats, config, snapshot, power, cloud_ref_model=cloud_ref_model
    )

    assumptions = _assumptions(config, snapshot, power, cloud_ref_model)
    receipts = compute_receipts(reports, month=m, assumptions=assumptions)
    return ReportBundle(
        reports=reports,
        receipts=receipts,
        pricing_origin=snapshot.origin,
        staleness=staleness,
        power=power,
    )


def _assumptions(
    config: Config,
    snapshot: pricing.PricingSnapshot,
    power: PowerReading,
    cloud_ref_model: str,
) -> list[str]:
    """The disclosures the receipts MUST carry so the numbers are honest."""
    out: list[str] = []
    out.append(f"cloud-equivalent priced at reference model '{cloud_ref_model}'")
    if power.mode == "unknown":
        out.append("energy cost OMITTED (power draw unknown — electricity unknown mode)")
    elif config.econ.electricity_usd_per_kwh is None:
        out.append("energy cost OMITTED ($/kWh not configured)")
    else:
        out.append(
            f"energy at ${config.econ.electricity_usd_per_kwh}/kWh, "
            f"power {power.mode} ({_fmt_num(power.watts)}W)"
        )
    if config.econ.hardware_amortization_usd_per_month is None:
        out.append("no hardware amortization included")
    else:
        out.append(
            f"hardware amortized at ${config.econ.hardware_amortization_usd_per_month}/mo"
        )
    out.append(
        f"cloud prices from {snapshot.origin} pricing.json "
        f"(newest as_of {snapshot.newest_as_of()})"
    )
    out.append(
        "true cost charges local retries AND cloud escalations for the same task"
    )
    return out
