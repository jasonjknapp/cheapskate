# Cheapskate Handoff

> **Last updated: 2026-07-22T01:21Z** (`feature/model-self-healing`, base `466bf7b`) — Global model self-healing is implemented and entering release review.

## 📌 Current State

- The public package now exposes model-independent job contracts, compatible role fallback, job-scoped incompatibility, dynamic recency-biased discovery, and protected LRU cleanup.
- Verification before release review: 365 tests passed; Ruff passed.

## ▶ Next Action

Run `/release-prep` on `feature/model-self-healing`; if it passes, run `/release-prod` and verify the live checkout is `main == origin/main`.

## 📐 Standing Directives

- **D1 — User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"
- **D2 — User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."
- **D3 — User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"
- **D4 — User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"
- **D5 — User goal (2026-07-21):** "The public cheapskate should get updated as well"
- **D6 — User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## 🟢 Active Workstreams

- `[cheapskate]` Model self-healing release review in progress (per D1–D6).

## 🧊 Cold Archive

- None yet.
