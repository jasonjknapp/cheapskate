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

## Acceptance

- A failed challenger never remains promoted; rollback is automatic and covered by a test.
- A rollback candidate resolves with its stored backend/endpoint/size.
- The public client behavior is unchanged (it does not wire this lifecycle).
