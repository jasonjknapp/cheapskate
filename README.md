# cheapskate

**Routes every task to the cheapest model that passes your quality bar (local or cloud) and
shows you the receipts.**

You installed Ollama, you pulled some good local models, and you *meant* to stop paying for the
easy stuff. But wiring "run this locally, escalate that to the cloud, and never send the sensitive
stuff off-box" into every tool is fiddly, and you have no idea how much you're actually saving,
so the local models sit idle and the cloud bill keeps coming.

Cheapskate is the **economics + judgment layer** that sits *above* your serving engines (Ollama,
MLX, LM Studio) and cloud APIs. It is not a gateway and not a serving engine. It decides *where
each task should run* (from a spend dial, per-task-type rules, and hard safety classes), then
measures what that decision cost **on your hardware** and hands you the receipts.

> **Status: v0.1, single-author, pre-1.0.** The core, econ engine, cloud tier, eval harness, and
> CI are in place and tested. It routes real work today. Read the [honest limits](#honest-limits)
> before you depend on it.

```
                        ┌──────────────────────────────────────────┐
   your tools           │                cheapskate                │
   (CLI agent,          │                                          │
    cron job,   ─────►  │   spend dial  ─┐                         │
    MCP client,         │                ├─►  router  ─► decision  │
    OpenAI SDK)         │   safety classes┘   (local | cloud |     │
                        │   (never-local /     refuse, fail closed )│
                        │    never-cloud)             │            │
                        │                             ▼            │
                        │   budget governor ◄── econ engine ◄──────┼── telemetry (content-free)
                        │   (per-user caps)     (measured $/task)   │
                        └───────────┬──────────────────┬───────────┘
                                    │                  │
                        ┌───────────▼──────┐   ┌───────▼──────────┐
                        │  serving engines │   │   cloud APIs     │
                        │  Ollama · MLX ·  │   │  OpenAI-compat · │
                        │  LM Studio ·     │   │  Anthropic       │
                        │  remote backends │   │  (BYO key, OFF   │
                        │  (other machines)│   │   by default)    │
                        └──────────────────┘   └──────────────────┘
```

Cheapskate never serves a model itself and never rebuilds a hundred-provider gateway. Serving stays
on Ollama/MLX; the cloud adapters are deliberately thin. It composes with what you already run.

## 10-minute quickstart

Everything below runs offline against the local checkout: no model, no server, no network, no
cloud key. It is the exact path proven from a bare clone on a fresh machine.

```bash
# 1. Clone and install (editable, with the dev/test tooling).
git clone https://github.com/jasonjknapp/cheapskate.git
cd cheapskate
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# 2. Preflight. Reports what's present (Python, dirs, serving engines, ports,
#    pricing-feed age). A missing Ollama/MLX is a WARN, not a failure. A bare
#    clone with nothing running still exits 0.
cheapskate doctor

# 3. Run the suite. No network, no live servers; everything is injected.
make test

# 4. Run the shipped deterministic eval set (the quality gate). Injected/offline
#    by default, so it proves green with no model attached; --live binds the real
#    broker client to gate an actual model.
cheapskate eval
```

`doctor` exits 0, `make test` is green, and `cheapskate eval` prints `"gate": "PASS"`. That is the
whole reproducibility contract: a stranger clones, installs, and proves the harness works before
attaching a single model. To route real work, start a serving engine (e.g. `ollama serve`), run
`cheapskate serve` to bring up the broker, then `cheapskate task` / point a tool at the endpoint.

## The spend dial

Routing policy is first-class, machine-readable state; one setting governs where work goes. Read
fresh on every decision; never cached.

| Dial | Meaning | Behavior |
|---|---|---|
| `0` | cloud-first | send local-capable work to the cloud (you want speed/quality over savings) |
| `1` | balanced | route by per-task-type floors |
| `2` | local-first *(default)* | prefer local; sub-dial `lite` \| `std` \| `max` tunes how hard it leans |
| `3` | local-only | never leave the machine |

```bash
cheapskate dial            # show the current dial + what it means
cheapskate dial set 2:max  # local-first, maximum patience before escalating
cheapskate dial set 0      # cloud-first for a crunch
```

The `2:max` sub-dial tells the verify-and-repair loop to tolerate a retry before escalating; the
other levels escalate fast. The dial is a state file, so a cron job, an editor, and an agent all
read the same policy.

## Receipts + the econ engine

This is the part nobody else packages. Every routed task logs a **content-free** telemetry event
(counts, durations, model, route, ok, never prompt or output text). The econ engine reads that
feed and computes what each route actually cost *on your hardware*:

- **tokens/sec per model per machine**, measured from live telemetry, not a spec-sheet guess;
- **energy cost** via `powermetrics` watt sampling (Apple Silicon) × your `$/kWh`, and honestly
  reports **"electricity unknown"** rather than fabricating a number when it can't sample;
- optional **hardware amortization** ($/month over your horizon), if you choose to model it;
- **cloud prices** from a bundled `pricing.json` (per-row source + `as_of`, refreshed weekly by
  CI, never fetched at runtime), so the local-vs-cloud comparison is against real list prices.

The honest part: the true-cost math **charges retries and escalations**. If a task drafts locally,
fails verification, retries, and finally escalates to the cloud, all of that is counted: the local
attempt was not free. Most "route to save money" tools quietly price only the happy path. The cost
math is deterministic and unit-tested precisely because that is the claim a skeptic will try to
break.

```bash
cheapskate econ            # per-task-type routing recommendation + true $/1M-token table
cheapskate report         # monthly receipts (routed %, quality pass, cost)
cheapskate report --share  # a content-free aggregate receipt, safe to post publicly
```

`--share` reads **only** numeric aggregates, model ids, and your machine id. It never touches a
free-text field, pinned by test so a poisoned telemetry line can't leak into a public receipt.

## Eval-gated model currency

New local models ship constantly. Cheapskate can auto-discover them (Hugging Face) but only
**promotes** what passes *your* eval suite on *your* hardware, with the incumbent, fallback, and
rollback targets protected from ever being pruned. Stop choosing models by vibes; choose them by
whether they pass your bar and what they cost you per token.

## Safety classes: both directions, fail closed

Two symmetric hard classes, enforced in the router *before* any dial logic runs:

- **`never_local`**: the task must not be answered by a local model, and there is **no silent
  cloud fallback**. It is a hard refusal. Defaults: `financial`, `legal`, `medical`, `credentials`.
- **`never_cloud`**: the task must never leave the machine. If the dial would send it off-box,
  that is a hard error (kept local or refused, never shipped). Defaults: empty (opt-in the task
  types your compliance posture requires).

Both **fail closed**: a `never_cloud` task with the local fleet down errors out. It does *not*
fall back to the cloud. A `never_local` task with no cloud tier configured errors out. It does
*not* quietly answer locally. Pinned by tests in both directions. This is the compliance story:
you can prove a class of work physically cannot leave the box.

## Cloud tier: BYO keys, OFF by default

Thin adapters, not a gateway: `openai-compat` drives any OpenAI-compatible API (OpenAI, OpenRouter,
a Gemini OpenAI-compat endpoint, a local vLLM); `anthropic` drives Claude. Every provider ships
**disabled**; a shipped install reaches the cloud only after you enable a provider *and* set its
`api_key_env` in the environment. Secrets live in environment variables, never in config, never in
the repo.

## Adoption surfaces

- **OpenAI-compatible endpoint.** Point any OpenAI-client tool's `base_url` at the broker's
  `/v1/chat/completions`. A `task_type` extension field opts a request into econ routing; without
  it, it's a plain role/model proxy.
- **MCP server.** `cheapskate mcp` (stdio) exposes `run_task` and `econ_report` to any MCP client
  (a code assistant, an agent CLI; needs the `mcp` extra).
- **Python API.** `cheapskate.client.complete()` / `generate_json()` go through the broker with
  graceful degradation.

## Multi-machine

**v0.1 (today): remote backends.** A backend entry with a non-localhost URL points at another box's
Ollama/MLX endpoint. The `machine_id` field flows through telemetry and the econ report, so tokens/sec
and watts are tracked per machine. Your desktop can dispatch to the GPU box in the other room today.

**v0.2 (roadmap): the fleet agent.** Remote load/swap, per-machine locks, auto-discovery: "your
household is now a fleet." Cheapskate schedules *tasks* across machines; it never shards a model
across them (that's a different tool's lane).

## How is this different from LiteLLM / TensorZero / RouteLLM / GPUStack?

Short version: they are all good at what they do, and none of them measures what a route costs on
your own hardware, gates local-model promotion on your evals, or enforces a never-cloud class. That
combination is the gap cheapskate fills. Be honest with yourself about what you actually need;
often the answer is one of these, or one of these *plus* cheapskate.

| | **cheapskate** | **LiteLLM** | **TensorZero** | **RouteLLM** | **GPUStack** |
|---|---|---|---|---|---|
| **What it is** | Economics + judgment layer above serving engines & gateways | AI gateway / proxy for 100+ LLM APIs | LLMOps platform: gateway + observability + optimization | Query-difficulty router (strong ↔ weak model) | GPU-cluster manager for model serving |
| **Routing basis** | Spend dial + per-task-type rules + safety classes | Load-balance / fallback across configured models | A/B + fallbacks/retries; optimizes prompts & models | Learned router predicts if a query needs the strong model | N/A (it serves; doesn't task-route) |
| **Local models** | First-class (the whole point) | Supported as just another provider | Supported (self-hosted via the gateway) | Supported as the weak model (e.g. via Ollama) | Serves them (vLLM/SGLang cluster) |
| **Measured on-your-hardware econ** (tokens/sec, watts, retries+escalations) | **Yes (the wedge)** | No (prices requests from a cost map) | No (optimization ≠ hardware cost) | No | Meters tokens/utilization, not $/task vs cloud |
| **Eval-gated local model promotion** | **Yes** | No | Has evals, but not for local-model promotion | Ships router evals, not model promotion | No |
| **never-cloud / never-local classes** (fail-closed) | **Yes, both directions** | Guardrails/budgets, not a fail-closed on-box class | No | No | No |
| **Serves models itself** | No (composes with Ollama/MLX/etc.) | No (it's a proxy) | No (it's a gateway) | No | **Yes** (that's its job) |
| **When to use THEM instead** | N/A | You need one API over 100+ cloud providers with per-key spend limits and virtual keys | You want production-metric-driven prompt/model optimization + deep observability | You want a research-grade learned router to auto-pick strong vs weak per query | You're standing up a multi-GPU serving cluster (LLMaaS) |

Compose, don't compete: run your serving on Ollama/MLX or GPUStack, keep LiteLLM if it's already
your cloud gateway (cheapskate can dispatch through an OpenAI-compatible endpoint), and let
cheapskate own the *where-should-this-run-and-what-did-it-cost* decision on top.

*(Competitor positioning verified against each project's own GitHub/docs on 2026-07-11. If a cell
is out of date, it's a bug; open an issue.)*

## Honest limits

Portfolio-grade honesty, because the alternative gets shredded on the first read:

- **v0.1, single author, no SLA.** Issues welcome; response is best-effort. Don't put it on a
  critical path you can't debug yourself.
- **The measured story is about *routing behavior*, not a dollar headline.** See
  [BENCHMARKS.md](BENCHMARKS.md): the author's ~8-day telemetry shows **~76% of real delegations
  served locally** (a bounded broker outage excluded and disclosed), a steep adoption ramp, and
  per-role success/latency. There are **no token fields in that telemetry**, so any dollar figure
  is an explicitly-labeled estimate, not a measured number. The receipts *mechanism* is real and
  tested; big savings claims are yours to generate on your own hardware, not ours to promise.
- **Apple-Silicon-first for energy.** `powermetrics` watt sampling is Apple Silicon; elsewhere the
  engine runs in honest "electricity unknown" mode.
- **Not built (yet):** the v0.2 fleet agent (remote load/swap), a web dashboard (a static HTML
  report from the JSONL is the v0.1 answer), sharded/distributed inference (out of scope, always;
  that's exo's lane), and a hosted service (there won't be one).
- **Single-large-model machines.** The safety semantics (machine-wide flock, de-load before a large
  load, never preempt a running generation) assume you run one big model at a time on a box. That
  matches a MacBook/desktop; a multi-GPU server wants GPUStack underneath.

## Roadmap

- **v0.1 (now):** core routing, econ engine + receipts, cloud tier, safety classes, eval-gated
  currency, remote backends, OpenAI-compatible + MCP surfaces, CI.
- **v0.2:** the fleet agent (remote load/swap, per-machine locks, auto-discovery); a community
  hardware-benchmark corpus (see [BENCHMARKS.md](BENCHMARKS.md); PRs open now).
- **Later, maybe:** an interactive local-vs-cloud calculator seeded from published aggregates.
  Explicitly *not* on the roadmap: sharded inference, a 100-provider gateway, a hosted service.

## More

- Architecture and conventions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Security model: [SECURITY.md](SECURITY.md)
- Measured benchmarks + community hardware table: [BENCHMARKS.md](BENCHMARKS.md)
- Contributing: [CONTRIBUTING.md](CONTRIBUTING.md)
- License: [Apache-2.0](LICENSE) (explicit patent grant, deliberate)

Built and maintained by [Jason Knapp](https://github.com/jasonjknapp).
