# Cheapskate architecture & conventions

One-liner: **routes every task to the cheapest model that passes your quality bar — local or
cloud — and shows you the receipts.** Cheapskate is the economics + judgment layer ABOVE serving
engines (Ollama, MLX, LM Studio) and cloud APIs. It never serves models itself and never rebuilds
a multi-provider gateway.

## Package map (v0.1)

```
src/cheapskate/
├── paths.py          XDG path helpers (config/state dirs) — the ONLY place paths are derived
├── config.py         config.yaml loader → Config object (shipped defaults + user overrides)
├── telemetry.py      content-free JSONL event writer (state_dir()/telemetry.jsonl)
├── broker/
│   ├── app.py        FastAPI app: auth classes, admission, /v1/chat/completions,
│   │                 /v1/embeddings, /v1/models, /admin/status
│   ├── gates.py      PriorityGate + ModelAwareGate (admission: a running generation is NEVER preempted)
│   └── capacity.py   capacity_decision(), memory_snapshot() (RAM budget vs resident models)
├── backends/
│   ├── resolve.py    role/model → backend spec (ollama | mlx | lmstudio | remote | cloud)
│   ├── mlx.py        ensure/stop MLX server; SINGLE-LARGE-MODEL lock (machine-wide flock)
│   ├── ollama.py     residency probes, stop/evict helpers
│   └── preflight.py  role preflight: de-load co-residents before a large load; release
├── router/
│   ├── dial.py       the spend dial: 0 cloud-first · 1 balanced · 2 local-first · 3 local-only
│   │                 (+ intensity sub-dial on 2: lite|std|max); read from state file, never cached
│   ├── routes.py     route_decision(task_type, dial, config) → local | cloud | refuse
│   │                 enforces never_local AND never_cloud task classes — both FAIL CLOSED
│   └── task.py       run(task_type, criteria, payload): delegate → parse envelope → verify hook
│                     (≤2 retries then escalate signal). The verify-and-repair primitive.
├── registry/
│   ├── registry.py   registry.yaml: roles → {model, backend, fallback, quarantine}; atomic writes
│   └── currency.py   discover (HF) → evaluate (YOUR eval suite) → promote/rollback;
│                     incumbent + fallback + rollback targets are NEVER pruned
├── cloud/
│   ├── __init__.py   public surface: dispatch_role(), provider_for_role(), CloudError
│   └── adapters.py   thin cloud tier: openai-compat + anthropic adapters (lazy SDK imports,
│                     env-only secrets, OFF by default) → uniform {text, model, tokens, latency}
├── mcp_server.py     stdio MCP server (cheapskate mcp): run_task + econ_report tools, thin
│                     over router/task + econ/report (needs the 'mcp' extra)
├── client.py         python API: complete(), generate_json() — via broker, graceful degrade
└── cli.py            argparse CLI: dial · models · task · serve · mcp · doctor · econ · report
```

The broker's `/v1/chat/completions` doubles as the drop-in OpenAI-compatible adoption surface: a
`task_type` extension field opts a request into econ routing (dial + safety classes + cloud
dispatch); otherwise it is a direct role/model proxy. `router/task.py` executes both local and
cloud routes (fail-closed both directions), consults the budget governor before a cloud dispatch,
and emits a `kind="generation"` event per attempt (the econ report/governor cost only that kind;
`kind="task.run"` is a per-run summary, not re-counted).

Shipped alongside the core: the deterministic eval harness (`cheapskate eval`), full `doctor`
preflight checks, and CI.

## Contracts (both extraction agents code against these)

- `paths.config_dir()` → `$XDG_CONFIG_HOME/cheapskate` (default `~/.config/cheapskate`);
  `paths.state_dir()` → `$XDG_STATE_HOME/cheapskate` (default `~/.local/state/cheapskate`).
  Created on demand. NO other module touches `os.environ` for paths or hardcodes a home path.
- `config.load()` → `Config` (pydantic model). Shipped defaults live in `config.py` as data;
  user file at `config_dir()/config.yaml` deep-merges over them. Key sections:
  `broker` (host, port=4747, keys), `dial` (default level, state file), `machine`
  (machine_id default = sanitized hostname, ram_budget_gb default = detected),
  `backends` (endpoints incl. remote URLs — a backend entry with a non-localhost URL IS the
  multi-machine story in v0.1), `task_types` (defaults + user-defined), `never_local`
  (default: financial, legal, medical, credentials), `never_cloud` (default: empty, documented),
  `users` (named profiles → key class, quotas).
- `telemetry.log_event(kind, **fields)` appends one JSON line. CONTENT-FREE BY CONSTRUCTION:
  never prompt/output text — only counts, lengths, durations, model, backend, machine_id,
  task_type, user, ok, retries, escalated, error kind. Every event carries `machine_id` and
  `ts` (UTC ISO). This is the raw feed the econ engine consumes — get the fields right.
- Broker auth: named keys with classes `interactive` > `background` (priority), per-user.
  Generic names only (no personal key names).

## Hard rules

1. This repo is fully self-contained and runnable on a stranger's machine: no imports, links,
   or references to any code outside the repo.
2. No personal residue in code, comments, docstrings, or tests: no personal names, private
   hostnames or domains, absolute home-directory paths, or phone/account fragments. Task-type
   lists and profiles ship as generic defaults.
3. Secrets via environment variables only. Nothing secret in config.yaml or the repo.
4. Telemetry is content-free (see contract). `.gitignore` already excludes all runtime state.
5. Single-large-model safety semantics are load-bearing: machine-wide flock, de-load before
   large load, a running generation is never preempted. Preserve them exactly; genericize only
   names and paths.
6. Style: Python ≥3.11, type hints on public functions, `httpx` preferred for HTTP (stdlib
   urllib acceptable where ported code already uses it), SPDX header
   `# SPDX-License-Identifier: Apache-2.0` on every file, ruff line-length 100.
7. Tests: pytest, no network, no live servers — fake clocks/processes/HTTP via injection points
   that already exist in the sources (runner=, killer=, api= params). Every ported invariant
   that had a pinning test upstream keeps one here.
