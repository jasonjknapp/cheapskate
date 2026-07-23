# Cheapskate Handoff

> **Last updated: 2026-07-23** (`feature/model-self-healing`, `2d3af4b`) — privacy/attribution patch committed and green; paused before the release gate. No PR, push, merge, deployment, model installation, or deletion occurred.

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
