# Cheapskate Handoff

> **Last updated: 2026-07-23** (`feature/model-self-healing`, `a41b4ef` + uncommitted hardening) — paused before the release gate; no PR, push, merge, deployment, model installation, or deletion occurred.

## 📌 Current State

- Public self-healing primitives and earlier adversarial-review remediations are committed through `a41b4ef`: semantic job contracts, bounded repair, exact-route same-role failover, declared capability enforcement, expiring job/model incompatibility, exact Ollama tag/capacity checks, guarded discovery/install/eval/promotion, quality-first ranking, notification receipts, deadline admission, and protected source-independent LRU planning.
- The current uncommitted patch applies one broker-enforced `X-Model-Privacy: never_cloud` contract to text and structured requests, resolves privacy at the broker after live model resolution, and pins each router attempt to `candidate.model`. A rich completion whose returned model differs from that candidate is rejected so quarantine/telemetry cannot misattribute a hidden fallback.
- Focused verification is **not green yet**: `70` tests ran; `66` passed and `4` failed. Two old injected callbacks in `tests/test_task.py` must accept the intentional `model=` keyword. Two `tests/test_client.py` cases expose a real error-message gap: all-nonlocal role candidates should raise the explicit verified-local-backend refusal rather than generic `NoCompatibleModel`.
- The prior 403-test/Ruff result applies only to the committed predecessor. Any prior adversarial review is invalidated; the fresh two-clean counter is zero.
- Machine runtime changes are in `/Users/jason/dev/.worktrees/agent-workflows/global-model-self-healing` at `f93106b` plus uncommitted changes. Atlas work is preserved on `fix/atlas-stash-conflict-recovery` at `35a5477`, but its recorded worktree is absent and `origin/main`/`origin/release` independently advanced to `237889a`; it needs a separate rebase/review decision.

## ▶ Next Action

1. Read `docs/plans/global-model-self-healing.md` and this handoff, then inspect the uncommitted diff; preserve it.
2. Add the explicit no-local-candidate refusal in `src/cheapskate/client.py`; update only the two injected callbacks in `tests/test_task.py` to accept `model=None` (the production contract must retain exact model selection).
3. Run `PYTHONPATH=src /Users/jason/dev/Personal/cheapskate/.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_client.py tests/test_task.py tests/test_task_cloud.py tests/test_broker_integration_smoke.py`, then full pytest and Ruff. Update this handoff and plan with actual counts and the new committed SHA.
4. Restart `/release-prep`: two fresh clean adversarial reviews on the exact SHA, PR, exact-PR-SHA review/staging gates, then `/release-prod` only if all gates pass. Jason already authorized progression after passing gates; never start paid CI.

## 📐 Standing Directives

- **D1 — User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"
- **D2 — User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."
- **D3 — User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"
- **D4 — User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"
- **D5 — User goal (2026-07-21):** "The public cheapskate should get updated as well"
- **D6 — User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## 🟢 Active Workstreams

- `[cheapskate]` Paused in release-prep with the uncommitted broker-privacy/attribution patch described above; four focused failures remain.
- `[agent-workflows]` Paired global broker/runtime patch is uncommitted and needs a privileged or hermetic rerun after its sandbox-only manager-lock failures.
- `[jknapp.com]` Atlas hardening branch is preserved but stranded from its worktree; reconcile with current `main`/`release` independently before release.

## 🧊 Cold Archive

- None yet.
