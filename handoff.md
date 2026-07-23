# Cheapskate Handoff

> **Last updated: 2026-07-23** (`feature/model-self-healing`, `b80377e`) — privacy/attribution patch committed + green; **Sol adversarial R1 found 5 real findings (2 P1 blockers, 2 P1 majors, 1 minor), ALL fixed at `b80377e`** (411 pass, Ruff clean); R2 fresh review running. No PR, push, merge, deployment, model installation, or deletion occurred.

## 🛡️ Adversarial review log
- **R1 (Sol `gpt-5.6-sol`, base main, 2026-07-23):** NOT clean. Findings + fixes at `b80377e`:
  - F1 [P1] task_type cloud route bypassed the privacy gate → broker now refuses cloud action under `never_cloud` (422) in `_route_task_type`.
  - F2 [P1] pre-check spec ≠ dispatched endpoint (prepare_backend re-resolves) → broker re-verifies `never_cloud` against the actual prepared base URL.
  - F3 [P1-major] `generate_json` role path didn't verify served-model identity → fails closed on mismatch; `complete()` exposes raw `served_model`.
  - F4 [P1-major] router accepted missing provenance (complete substituted requested model) → fails closed on missing/mismatched `served_model`.
  - F5 [minor] exhausted run attributed to incumbent → now attributes to the last model tried.
  - Tests added: F1/F2 (broker smoke), F3 (client), F5 (task); F4 covered by updated token-count + existing mismatch tests.
- **R2 (Sol, base main, on `b80377e`):** NOT clean — 4 findings, ALL fixed at `a89c492` (416 pass, Ruff clean, +5 tests):
  - R2-1 [P1] a config-policy `never_cloud` task_type forced local could still egress if its pinned role resolved remote and the header was absent → broker threads a `privacy_override` so the policy enforces `never_cloud` header-free.
  - R2-2 [major] a quality-then-transport failure cross-attributed the incumbent's output to the fallback (regression from R1's F5) → router resets `last_env` per candidate.
  - R2-3 [major] `_candidate_installed` filtered out lmstudio/remote role candidates → now eligible (no downloadable artifact; reachability checked at invoke).
  - R2-4 [minor] unknown role raised internal `LocalUnavailable` → normalized to public `CheapskateUnavailable` in `complete()` + `generate_json()`.
- **R3 (Sol, base main, on `a89c492`):** NOT clean — 2 P1 regressions the self-healing candidate filter introduced, both fixed at `c28f2cf` (419 pass, Ruff clean, +3 tests):
  - R3-1 [P1] an uninstalled Ollama incumbent under `machine.auto_pull` was filtered out (raise, no HTTP) instead of being gate-pulled → auto_pull candidates eligible again (allowance at the call site; `_candidate_installed` stays a pure probe).
  - R3-2 [P1] a custom role that declares no capabilities had every candidate filtered → undeclared caps now assumed to satisfy the required set; `RoleEntry` gains an optional `capabilities` field (Pydantic no longer strips it).
- **R4 (Sol, base main, on `c28f2cf`):** NOT clean — 4 findings (3 P1, 1 minor). **Reviewer confirms the privacy gates are "substantially hardened"**; the remaining findings moved to the broader self-healing engine lifecycle:
  - R4-1 [P1] string-return `complete=` adapters skip the identity check (production uses dicts → already fail-closed; string path is legacy/test, can't carry provenance — contract tension).
  - R4-2 [P1] a promoted challenger that fails its first live invocation is not rolled back (engine `run()` has no rollback callback). **Latent: the public client does not wire `promote`/`discover` (client.py:521), so this lifecycle is not on the release surface.**
  - R4-3 [P1] `role_candidates` resolves a rollback entry by model string only, losing stored backend/endpoint from `rollback_configs`. **Also only reachable once promotion populates rollbacks — not on the public client surface.**
  - R4-4 [minor] `role_candidates` raises `backends.resolve.LocalUnavailable`, not `router.task.LocalUnavailable`, for a task referencing a missing role (trivial normalization; on the used path).
- **JASON DECISION 2026-07-23 → SCOPE A (ship privacy core).** Executed the scope-A cleanup at `a549fd9` (420 pass, Ruff clean): R4-4 fixed (router surfaces `router.task.LocalUnavailable`), R4-1 documented (bare-string completion is a trusted-adapter contract; provenance enforcement stays on the fail-closed dict path). The latent promote/rollback lifecycle findings (R4-2/R4-3) are deferred to [`docs/specs/self-healing-lifecycle-hardening.md`](docs/specs/self-healing-lifecycle-hardening.md) — they are NOT wired into the public client (`client.py:521` calls `engine.run` without promote/discover).
- **R5 (scoped 2-clean, first pass):** running on `a549fd9`, lens explicitly scoped to the privacy/attribution/eligibility surface with the lifecycle findings marked out-of-scope. Need R5 + R6 both clean → push + PR → STOP at exit gate for Jason's go.

## 📌 Current State

- Public self-healing primitives and earlier adversarial-review remediations are committed through `a41b4ef`: semantic job contracts, bounded repair, exact-route same-role failover, declared capability enforcement, expiring job/model incompatibility, exact Ollama tag/capacity checks, guarded discovery/install/eval/promotion, quality-first ranking, notification receipts, deadline admission, and protected source-independent LRU planning.
- The privacy/attribution patch is now committed at `2d3af4b`: one broker-enforced `X-Model-Privacy: never_cloud` contract on text and structured requests, privacy resolved at the broker after live model resolution, each router attempt pinned to `candidate.model`, and a rich completion whose returned model differs from that candidate rejected so quarantine/telemetry cannot misattribute a hidden fallback. `generate_json()` also raises the explicit verified-local-backend refusal (`_never_cloud_role_has_local_candidate`) when a role has no local serving candidate, instead of a generic `NoCompatibleModel`.
- Verification is **green**: focused `70 passed`; full suite `407 passed`; Ruff clean.
- Any prior adversarial review is invalidated by this commit; the fresh two-clean counter is zero.
- Machine runtime changes are committed at `/Users/jason/dev/.worktrees/agent-workflows/global-model-self-healing` `6e4a2ba` (full suite `2530` OK, boundary check passes). Atlas work is preserved on `fix/atlas-stash-conflict-recovery` at `35a5477`, but its recorded worktree is absent and `origin/main`/`origin/release` independently advanced to `237889a`; it needs a separate rebase/review decision.

## ▶ Next Action

1. Restart `/release-prep` independently for Cheapskate: two fresh clean adversarial reviews on the exact SHA `2d3af4b`, PR, exact-PR-SHA review/staging gates, then `/release-prod` only if all gates pass. Jason already authorized progression after passing gates; never start paid CI.
2. Same for agent-workflows `6e4a2ba`.
3. Reconcile Atlas (`fix/atlas-stash-conflict-recovery` `35a5477`) against `origin/main`/`origin/release` `237889a` separately before any Atlas release claim.

## 📐 Standing Directives

- **D1 — User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"
- **D2 — User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."
- **D3 — User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"
- **D4 — User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"
- **D5 — User goal (2026-07-21):** "The public cheapskate should get updated as well"
- **D6 — User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## 🟢 Active Workstreams

- `[cheapskate]` Broker-privacy/attribution patch committed and green at `2d3af4b`; paused at the start of the release gate (two fresh clean reviews required).
- `[agent-workflows]` Paired global broker/runtime patch committed and green at `6e4a2ba` (2530 unittests OK, boundary check passes); paused at the start of its independent release gate.
- `[jknapp.com]` Atlas hardening branch is preserved but stranded from its worktree; reconcile with current `main`/`release` independently before release.

## 🧊 Cold Archive

- None yet.
