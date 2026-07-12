# Contributing

Thanks for looking. A few honest words on what this project is before you invest time.

## Maintenance stance

Cheapskate is a **portfolio-grade project maintained by one person**. That means:

- **Issues and PRs are genuinely welcome** — bug reports, benchmark rows, doc fixes, and
  well-scoped features all help.
- **There is no SLA.** Response is best-effort and may take a while. If something is on your
  critical path, fork it and don't wait on me.
- **Scope is held deliberately.** The [non-goals](README.md#roadmap) are non-goals on purpose:
  no sharded/distributed inference, no hundred-provider gateway, no hosted service, no web
  dashboard in v0.1. A PR that adds one of those, however good, is likely to be declined — not
  because it's bad, but because it changes what this tool *is*. Open an issue to discuss before
  building anything large.

The best first contribution is a **benchmark row** (below) or a bug with a failing test.

## Adding a benchmark row

The [community hardware table](BENCHMARKS.md#community-hardware-table) grows by PR. To add your
machine:

1. Run some real work through cheapskate on the model you want to report, then
   `cheapskate econ` — it reports measured tokens/sec per model per machine from your own
   telemetry. Use that number, not a spec sheet.
2. If you're on Apple Silicon with `$/kWh` configured, cheapskate samples watts via
   `powermetrics`; use that figure. **If you can't measure watts, put `n/a`** — an honest gap is
   worth more than a guess.
3. Add exactly one row per `(machine, model)` to the table in `BENCHMARKS.md`, matching the
   template columns: machine, RAM, model tag, backend, tokens/sec, watts, source.
4. Link your handle or a short gist backing the numbers, so a reader can reproduce.

Rows with invented or spec-sheet numbers will be asked for a source. The whole point of the table
is *measured, not estimated*.

## Running the suite and evals

Everything runs offline against the local checkout — no network, no live servers, no cloud key.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

make test     # full pytest suite (no network, no live servers — everything injected)
make lint     # ruff check
make eval     # shipped deterministic eval set, injected/offline (the quality gate)
make doctor   # full preflight (WARN-passes on a bare machine)
make check    # lint + test + eval + doctor — the CI gate, run locally
```

Point `make` at a specific interpreter with `PYTHON=.venv/bin/python` if needed. CI runs the same
`make check` on Python 3.11 and 3.12; a green local `make check` is the bar for a PR.

## Code conventions

These are enforced (ruff + tests) and non-negotiable in a PR:

- **Python ≥ 3.11**, type hints on public functions, `httpx` preferred for HTTP.
- **SPDX header** `# SPDX-License-Identifier: Apache-2.0` on every source file.
- **ruff line-length 100**; `make lint` must be clean.
- **Tests: pytest, no network, no live servers.** Fake clocks/processes/HTTP through the injection
  points that already exist (`runner=`, `killer=`, `api=`, `complete=`, `cloud_dispatch=`,
  `verify=` params). Every ported invariant keeps a pinning test.
- **Telemetry stays content-free** (see [SECURITY.md](SECURITY.md)). A change that logs a
  content-bearing field will fail the suite by design.
- **No personal residue** — no personal names, private hostnames/domains, absolute home paths, or
  account fragments in code, comments, tests, or docs. Defaults ship generic.
- **Secrets via environment variables only.** Nothing secret in config or the repo.

## Pull requests

- Branch from the current default branch; keep PRs focused (one concern per PR).
- Include tests for behavior changes; update the relevant doc in the same PR (one destination per
  kind of change — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- Make sure `make check` is green before you open the PR.

## License

By contributing you agree your contributions are licensed under the project's
[Apache-2.0](LICENSE) license (which carries an explicit patent grant — that's deliberate).
