# Cheapskate Handoff

> **Last updated: 2026-07-22** (`feature/model-self-healing`) — implementation and deterministic verification complete; `/release-prep` adversarial review is running.

## 📌 Current State

- Public self-healing primitives and adversarial-review remediations are complete through `f1b705a`: semantic job contracts, bounded repair, verified-installed same-role failover, declared capability enforcement, expiring job/model incompatibility, end-to-end never-cloud locality for structured and text paths, guarded discovery/install/eval/promotion, quality-first ranking, adapter-isolated notification receipts, monotonic deadline admission, and protected source-independent LRU planning.
- The public API remains source-compatible. A malformed HTTP 200 completion now changes candidates instead of escaping failover; dynamic discovery cannot serve until fit, eval, quality-floor, and promotion gates pass.
- Verification is green: 400 tests, Ruff, diff hygiene, and the required local code-model review. The prior adversarial findings were remediated and the two-clean counter has restarted from zero on this checkpoint.
- Machine-specific implementation lives in the paired agent-workflows release; Atlas/public-article hardening lives in the paired jknapp.com release. Nothing has been pushed, merged, deployed, or run live yet.

## ▶ Next Action

Complete two fresh clean adversarial passes, open the PR, rerun the exact-PR-SHA gate, then execute `/release-prod` under Jason's explicit authorization if every gate remains green.

## 📐 Standing Directives

- **D1 — User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"
- **D2 — User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."
- **D3 — User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"
- **D4 — User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"
- **D5 — User goal (2026-07-21):** "The public cheapskate should get updated as well"
- **D6 — User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## 🟢 Active Workstreams

- `[cheapskate]` Release-prep gate in progress on the verified feature branch.
- `[agent-workflows]` Paired global runtime/callsite release in progress independently.
- `[jknapp.com]` Paired public article + autonomous Atlas recovery release in progress independently.

## 🧊 Cold Archive

- None yet.
