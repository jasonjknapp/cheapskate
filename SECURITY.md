# Security model

Cheapskate routes tasks between local models and cloud APIs, so its security posture is about three
things: **where data is allowed to go**, **what leaves the machine in telemetry**, and **how
secrets and network surface are handled**. All three are designed to **fail closed** — when a
guarantee can't be met, the safe behavior is to refuse, not to guess.

This is a single-author v0.1 project with no formal SLA. If you find a vulnerability, open a GitHub
issue (or, for something sensitive, mark it and I'll follow up). Please don't expect a same-day
response.

## Data-egress classes — both directions, fail closed

Two symmetric hard classes are enforced in the router (`router/routes.py`) **before any spend-dial
logic runs**, so no dial setting can route around them:

- **`never_local`** — the task must not be answered by a local model, and there is **no silent
  cloud fallback**. It resolves to a hard refusal (`NeverLocal`), not a downgrade. Shipped
  defaults: `financial`, `legal`, `medical`, `credentials` — work you want kept on the strong tier
  by policy.
- **`never_cloud`** — the task must **never leave the machine**. If the dial would otherwise send
  it off-box (e.g. dial level 0, cloud-first), that is a hard error (`NeverCloud`) — the work is
  kept local or refused, never shipped. Shipped default: empty; opt in the task types your privacy
  or compliance posture requires.

If a task type is (mis)listed in both, **`never_local` wins** — refusing is the safe direction.

**Fail-closed is tested, both ways:**

- `test_never_local_fails_closed_at_every_dial` / `test_never_local_raises_no_local_no_cloud` — a
  never-local task refuses at every dial and never silently falls back to the cloud.
- `test_never_cloud_at_level_0_refuses_not_ships` / `test_never_cloud_forced_local_runs_local` — a
  never-cloud task stays on-box when local is possible, and **refuses rather than shipping off-box**
  when the dial is cloud-first.
- `test_cloud_route_no_provider_hard_errors` — a cloud route with no provider configured hard-errors
  rather than quietly answering locally.

The practical guarantee: you can define a class of work that **physically cannot leave the
machine**, and prove it with a test.

## Content-free telemetry — by construction

The econ engine needs a data feed, and that feed is designed so it **cannot carry your content**.

`telemetry.log_event()` writes one JSON line per event with counts, durations, model, backend,
`machine_id`, `task_type`, route, `ok`, retries, and error *kind* — and nothing else. Content-bearing
field names (`prompt`, `output`, `content`, `text`, `messages`, `payload`) are refused **by
construction**: a caller that tries to log one hits an assertion in tests/debug and, even with
assertions stripped in production (`-O`), the field is filtered out before write. Content never
lands either way.

The public **`--share`** receipt (`cheapskate report --share`) is the highest-risk surface — it's
meant to be posted publicly — so it's held to a stricter rule: it reads **only** numeric aggregates,
model ids, and your `machine_id`, and never touches a free-text telemetry field at all. Even a
*poisoned* telemetry line can't leak.

**Pinned by name:**

- `test_forbidden_content_field_refused` — every forbidden field name raises loudly.
- `test_no_content_leaks_even_when_mixed` — the scrubber drops content before write.
- `test_off_switch` — `CHEAPSKATE_TELEMETRY_OFF=1` disables the feed entirely.
- `test_share_never_emits_poisoned_content` — a telemetry line stuffed with a secret and an API-key
  string never surfaces in `--share`.
- `test_share_poisoned_task_type_and_model_are_scrubbed` — even the identifier fields `--share` *is*
  allowed to read get control chars stripped, so a poisoned id can't inject markdown/newlines.
- `test_share_only_reads_allowlisted_fields` — structural pin: the share-safe field allowlist
  excludes every known content-bearing field name.
- `test_generation_events_are_content_free` — the router's per-attempt generation events carry no
  content.

Telemetry lives in your local state dir (`state_dir()/telemetry.jsonl`), is `.gitignore`d from day
one, and never leaves your machine unless *you* run `--share` and post the result.

## Secrets — environment only

Cloud provider API keys are read from **environment variables only**, named by each provider's
`api_key_env` in config. A secret never lives in `config.yaml`, never in the repo, and never in
telemetry. Cloud providers ship **disabled**; an install reaches the cloud only after you enable a
provider *and* set its key in the environment — a deliberate two-step opt-in. The broker's keys
file is referenced out of band and expected at mode `600`; key names in config are generic (no
personal identifiers).

## Network surface — loopback by default

The broker binds **`127.0.0.1` by default** (`bind_loopback: true`). Reaching it from other machines
on your LAN or tailnet is a deliberate opt-in (`bind_lan: true`, which binds `0.0.0.0`) — nothing is
exposed off-host unless you choose it. Remote backends (the multi-machine story) are *outbound*
dispatch to another box's serving endpoint that you configure explicitly; there is no auto-discovery
in v0.1. The broker authenticates callers with named keys carrying a priority class (interactive >
background); a running generation is never preempted.

## Threat model, honestly stated

- **In scope:** accidental content leakage through telemetry or shared receipts; a sensitive task
  class silently escaping to the cloud; secrets landing in the repo or config; the broker being
  reachable off-host by default. Each of these is guarded and tested.
- **Out of scope for v0.1:** a hardened multi-tenant auth system, transport encryption between
  broker and remote backends (run them over a trusted network / tailnet), sandboxing of the models
  themselves, and defending a machine an attacker already controls. This is a personal-fleet tool,
  not a hosted service.

## Related reading

The design of the unattended-agent send-authority firewall (why automated agents get *no*
outward-facing capability surface) is written up as a companion article — linked from the project
homepage. It's the same fail-closed instinct applied to a different blast radius.
