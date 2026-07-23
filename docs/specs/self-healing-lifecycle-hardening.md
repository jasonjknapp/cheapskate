# Self-Healing Lifecycle Hardening (deferred)

> **Status:** OPEN — deferred from the never_cloud/attribution release (2026-07-23).
> **Origin:** adversarial review round 4 (Sol `gpt-5.6-sol`) of `feature/model-self-healing`.
> **Why deferred:** these findings are in the self-healing engine's discovery/promote/rollback
> lifecycle, which the **public client does not wire** (`client.generate_json` calls
> `SelfHealingEngine.run()` without `discover`/`promote`/`install`/`evaluate` — see
> `src/cheapskate/client.py`). They are real correctness bugs but are **not on the shipped
> public client's execution surface**, so the never_cloud/attribution release proceeds
> without them and they are tracked here for a dedicated hardening pass.

## Scope

Only reachable when a caller wires the promotion lifecycle into `SelfHealingEngine.run()`
(passing `discover` + `promote`, and populating `rollback_configs` via promotion). The
machine orchestrator does this; the public client does not.

## Findings to fix

### H1 — Roll back a challenger when live invocation fails (P1)

`src/cheapskate/self_healing.py` (post-`promote` path in `run()`): a discovered challenger can
pass `evaluate`, mutate the registry through `promote`, then fail its first real invocation or
schema validation in `_try_candidates`. The engine records the failure and eventually raises
`NoCompatibleModel` **without restoring the prior incumbent**, leaving the failed challenger
promoted despite the automatic-rollback contract.

- Fix direction: add a `rollback` callback to `run()` (symmetric with `promote`), and invoke it
  when a just-promoted challenger fails its first live `_try_candidates` attempt, before
  continuing/raising. Callers that pass `promote` must pass `rollback`.
- Test: promote succeeds → invoke fails → assert the incumbent is restored and a
  `model_rollback` notification fires.

### H2 — Reuse rollback snapshots when constructing candidates (P1)

`src/cheapskate/backends/resolve.py` `role_candidates()`: a rollback entry is resolved by model
**string only**, losing the full backend/endpoint/size that promotion stored in
`rollback_configs`. A former LM Studio incumbent such as `vendor/model` is then mis-inferred as
MLX (slash in the name), loses its endpoint and size, and is filtered out or dispatched
incorrectly instead of trying the valid loaded rollback. Custom remote endpoints and auto-pull
sizing are lost the same way.

- Fix direction: when resolving a rollback model, prefer its stored `rollback_configs` snapshot
  (backend/endpoint/approx_gb) over string-only inference.
- Test: promote populates a rollback with an lmstudio backend + loopback endpoint → the rollback
  candidate resolves with that backend/endpoint, not MLX inference.

### H3 — Quarantine-aware role candidate selection in `complete()` (P1, review R5)

`src/cheapskate/client.py` `complete()`: for a role request it seeds
`candidates = [(None, role)]` (a live broker role-resolve) then extends with
`role_candidates(role)[1:]`. When the incumbent is globally quarantined,
`role_candidates()` has already dropped it, so `[1:]` drops the first eligible
**fallback**, and the `(None, role)` live request resolves (via `resolve()`, which does
NOT consult quarantine) straight back to the quarantined incumbent — serving the
known-bad model and never trying the fallback.

- Why deferred: the obvious fix (drive `complete()` off the full quarantine-aware
  `role_candidates()` list by explicit model) **changes the client↔broker wire contract** —
  the first request stops sending `model: "role:<name>"` (broker-authoritative live role
  resolution) and sends a client-resolved concrete model instead. That is a deliberate design
  decision (who owns role→model resolution, client or broker?) with test-contract impact, not a
  convergence-loop patch. Needs a decision + careful test reconciliation.

### H4 — Fail-closed served-model provenance on the explicit-model `generate_json` path (P1, review R5)

`src/cheapskate/client.py` `generate_json(model=...)` (the non-role branch): parses and returns
without the served-model identity check the role path enforces, so a backend-side fallback can
satisfy an exact-model public call while its output is attributed to the requested model.

- Why deferred: adding the check is correct but many existing explicit-model tests use a fixed
  response model (`_chat_body` default `test-model`) that does not match the requested model, so
  it churns a broad test surface. Belongs with a deliberate pass that updates those tests to serve
  the requested identity (as the router/token-count tests already were).

## Already shipped in the release (NOT deferred)

- **R5-1 — env-proxy egress:** `never_cloud` now builds the httpx client with `trust_env=False`
  (client `_post_chat`) and the broker's httpx client is `trust_env=False` (it only dials local
  backends), so a private prompt cannot tunnel through an env-configured proxy despite a loopback
  URL. Covered by tests.
- **R5-4 — typed-config resolution:** `RoleEntry.endpoint` added and `_config_backends` reads
  `BackendEntry.url`, so remote/lmstudio role candidates resolve to their real endpoint instead of
  the Ollama localhost default. Covered by tests.

## Acceptance

- A failed challenger never remains promoted; rollback is automatic and covered by a test.
- A rollback candidate resolves with its stored backend/endpoint/size.
- `complete()` never serves a quarantined incumbent and never drops an eligible fallback.
- The explicit-model `generate_json` path fails closed on a served-model mismatch.
- The public client behavior is unchanged where not explicitly redesigned (wire-contract change in
  H3 is a deliberate, documented decision).
