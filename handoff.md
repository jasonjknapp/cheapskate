# Cheapskate Handoff

> **Last updated: 2026-07-22T02:47Z** (`feature/model-self-healing`) — Paused during release-review remediation at Jason's request.

## 📌 Current State

- Baseline implementation is stored at `3f133c2`; its pre-review verification was 365 tests passed and Ruff passed.
- Independent review then proved five release blockers: missing privacy enforcement, permanent compatibility quarantines, incomplete LRU metadata protection, discovery without mandatory fit/eval/promotion gates, and quality/latency ordering plus incomplete live integration.
- A WIP remediation adds those guards and begins wiring persistent job compatibility into the live client/router. It is intentionally marked WIP: tests and a fresh two-clean review have not run since these edits.
- Agent-workflows is stored separately at `2ea4716`. Its review proved additional machine-layer blockers (Owaves pre-gate, mismatched JJacked eval schema, incomplete fallback/rollback LRU protection, missing recovery/rollback notifications, and candidate-specific capability enforcement). Another live session owns that repository lock, so none of those findings were remediated in this paused turn.
- Nothing has been pushed, opened as a PR, merged, deployed, or run live.

## ▶ Next Action

Resume by claiming both repositories. First finish and test the Cheapskate WIP remediation. Then remediate the stored agent-workflows findings after its other live lock clears. Restart `/release-prep` review from the beginning for both exact new HEADs; run `/release-prod` only if every gate passes.

## 📐 Standing Directives

- **D1 — User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"
- **D2 — User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."
- **D3 — User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"
- **D4 — User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"
- **D5 — User goal (2026-07-21):** "The public cheapskate should get updated as well"
- **D6 — User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## 🟢 Active Workstreams

- `[cheapskate]` Paused WIP remediation after a blocking release review (per D1–D6).
- `[agent-workflows]` Paused at `2ea4716`; remediation blocked by another live repository claim.

## 🧊 Cold Archive

- None yet.
