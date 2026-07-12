# Benchmarks

Two things live here: (1) the **author's measured routing aggregates** from real use — the honest
launch numbers, not a marketing headline; and (2) a **community hardware table** for you to add
your machine to via PR. Numbers beat vibes; that's the whole ethos.

## What "measured" means (and what it doesn't)

The aggregates below come from ~8 days of the author's own local-routing telemetry. Read the
caveats before you quote anything:

- **Window is short and disclosed.** ~8.2 days (2026-07-04 → 2026-07-12 UTC), 8,474 raw events.
  This is early-adoption data, not a steady-state year. The trend matters more than any single day.
- **No token fields exist in this telemetry.** So there is **no measured dollar figure** here. Any
  token/dollar number is a labeled estimate from a fixed per-role budget, and we don't lead with it
  — the defensible story is *routing behavior*, not savings.
- **A bounded outage is excluded and disclosed, not hidden.** A single ~23-minute broker/pool
  outage on 2026-07-11 produced a retry flood (~6,700 failed rows, one user, one model, machine-gun
  502s). That is a stuck-server artifact, not real demand. Every headline rate is given
  **incident-excluded** — the defensible view — and we say so out loud.
- **Aggregates only.** No prompt text, no outputs, no paths, no raw rows. Computed locally from a
  content-free JSONL feed.

## Measured routing aggregates (author's machine, ~8 days)

### Served-vs-failed split (incident-excluded, 1,681 delegations)

| Bucket | Count | Share |
|---|---:|---:|
| served locally | 1,275 | **~76%** |
| failed (local attempt errored) | 402 | ~24% |
| fallback-direct / escalated | 4 | ~0.2% |

Outside the 23-minute outage, the fleet **served ~76% of delegations locally**. (Across *all* rows
including the outage the served rate reads ~19% — that number is an artifact of the retry flood and
is not quoted as the served rate.)

### Adoption ramp (locally-served calls/day, incident-excluded)

```
07-04   41
07-05   31
07-06   13
07-07   31
07-08  116
07-09  162
07-10  485   ← broker adoption inflects
07-11   96   (outage day, partial)
07-12  300   (partial day)
```

The clean growth signal is the 07-08 → 07-10 curve (116 → 162 → 485) as the broker went from
optional to default. The story is a ramp, not a static rate.

### Per-role behavior (incident-excluded; latency over successful calls)

| Role | Calls | Success | Median latency | p90 latency |
|---|---:|---:|---:|---:|
| classification | 540 | 65.6% | queue-inflated* | queue-inflated* |
| reasoning | 430 | 64.2% | 13.4 s | 54.1 s |
| code | 333 | **93.1%** | **2.5 s** | 88.1 s |
| domain_expert (custom role) | 170 | **100%** | 34.6 s | 138.2 s |
| vision_pipeline | 6 | 66.7% | 92.2 s | 139.1 s |
| creative | 6 | 100% | 20.6 s | 49.2 s |

\* Classification *latency* is inflated by requests queued behind the outage and is not
representative of a healthy 9B classify (sub-second to low-seconds). Its call *count* and success
rate are sound; its latency is not — so it isn't quoted.

The workhorse signal: **code — 333 calls, 93% success, 2.5 s median** — is the clean, high-value
result. A custom domain-specific role ran 170/170 at 100% — evidence that a well-fit local model
nails a narrow task.

### Retry / escalation rate

Explicit retry/escalation was barely instrumented in this window (the verify-then-escalate path was
new): 1 escalate marker, 6 fallback-direct rows total. The derivable rate (~0.1%) reflects **sparse
instrumentation, not a hardened reliability metric** — do not read it as "escalations almost never
happen." The meaningful reliability signal is the served-vs-failed split above.

### Estimated tokens offloaded (LABELED ESTIMATE — no measured tokens)

With a conservative fixed per-role (input, output) budget applied only to successfully-served-local
calls (1,275 calls, incident-excluded): **≈ 2.44 M tokens** handled locally over ~8 days
(≈1.79 M in / ≈0.65 M out). Priced against two reference tiers, the avoided cloud spend lands
**between ≈$0.66 (budget tier) and ≈$15 (mid tier)** for the window. The wide band *is the point*:
at this volume the value is token/rate-limit avoidance and privacy, not a large raw dollar figure.
This is an estimate with every assumption stated; it is not a measured saving and is not a headline.

## Community hardware table

Add your machine. This is the easiest first contribution — one row, real numbers from your own
`cheapskate` run. See [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-benchmark-row) for how to measure.

| Machine | RAM | Model | Backend | tokens/sec | Watts | Source |
|---|---|---|---|---:|---:|---|
| *(PR template — copy this row)* | e.g. 64 GB | e.g. `qwen3-coder:30b` | ollama / mlx | e.g. 42.0 | e.g. 65 (or `n/a`) | your handle / link |

Rules for a row:

- **Real numbers only.** `tokens/sec` from your own `cheapskate econ` or a documented measurement,
  not a spec sheet. If you can't measure watts, put `n/a` — an honest gap beats a guess.
- **Name the model tag and backend** exactly (`qwen3-coder:30b`, `ollama`), so a reader can
  reproduce.
- **One row per (machine, model)**. Link to your handle or a gist backing the numbers.

## Method notes

- **Source feed:** `cheapskate`'s content-free telemetry JSONL (`state_dir()/telemetry.jsonl`).
  Fields are counts/durations/model/route/ok — never content. See [SECURITY.md](SECURITY.md).
- **Delegation set** excludes admin/probe rows (health probes, registry ops) and eval/benchmark
  rows (the model-promotion pipeline) — those aren't production offload.
- **Latency percentiles** are over successful (`ok=true`) calls only.
- **Incident window** = 2026-07-11 04:00–06:00 UTC, excluded from every headline rate and disclosed.
- **Token/dollar figures** are heuristic estimates because the source has no token fields; every
  assumption is stated inline above. Once your telemetry carries token counts, `cheapskate econ`
  reports measured — not estimated — costs.
