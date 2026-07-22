<!-- PLAN-STATE v1 -->
current_phase: release-prep
phase_status: in_progress
last_commit:  1e08d5cbd01fda9cb73e7fdc323d98581dc8619a
next_action:  Complete two fresh clean adversarial passes, then open the release PR.
-->

# Global Model Self-Healing

## Why

> **User goal (2026-07-21):** "Jobs should be model independent to the degree reasonable. Substituting a different model should still work. If it is truely incompatible, can the job switch to an installed model that will work or ask the orchestrator to install a compatibel model and use that? These jobs should be self healing"

> **User goal (2026-07-21):** "I would like it to be fully autonomous, but it should notify me. Preapproved would mean a fixed, and therefore outdated list. It should find the latest top rated models with a strong recency of release bias and pick the best for the job from those."

> **User goal (2026-07-21):** "Agree with your recommendation. But older / least recently used models should be automatically deleted to make space"

> **User goal (2026-07-21):** "latency or resource use are not nearly as important as quality as long as it runs on this machine,"

> **User goal (2026-07-21):** "I'd just implement globably. The risk is low. And there's a real risk of losing track and not globablizing this later if we dont get it done now."

> **User goal (2026-07-21):** "Make sure you understand the orchestrator and relevant jobs so that the end result if a well functioning system. The public cheapskate should get updated as well"

> **User correction (2026-07-21):** "Requiring proof the model can be downloaded again creates a fail state where outdated models clog up the filesystem. I would not require that."

## Scope

This plan covers the public Cheapskate product. The machine-specific agent-workflows implementation cites this contract and adds its scheduled-job inventory, launchd integration, daily-brief repairs, and notification delivery without duplicating the public design.

## Locked design

1. Jobs declare capabilities, output contracts, quality gates, deadlines, and privacy requirements. They never depend on a concrete model name.
2. Runtime recovery classifies transport, availability, timeout, schema, safety, source-data, and quality failures separately.
3. Recovery attempts bounded repair, then a proven compatible installed model, then autonomous discovery, install, canary evaluation, and promotion.
4. Discovery has no fixed model or publisher catalog. Current popularity, benchmark evidence, and release recency nominate challengers; local job-specific evaluation decides promotion.
5. Quality dominates ranking. Latency and resource use are tie-breakers after machine fit and delivery constraints.
6. The scheduler learns runtime, starts work earlier, and may use a bounded late-delivery window rather than silently lower quality.
7. Incumbent, active challenger, in-use models, and one rollback per role are protected. Other managed models are removed by role-aware least-recently-used eviction; upstream availability is never a deletion gate.
8. Overrides expire into a fresh selection pass; they never revert blindly to an older model. Candidate quarantines are scoped and expire.
9. Failed recovery and rollback notify immediately. Successful installs, promotions, schedule changes, and pruning are summarized.
10. Existing callers remain source-compatible while the global job inventory migrates.

## Phases

### Public core

- Job contracts and failure taxonomy.
- Compatibility history and candidate ordering.
- Contract-aware text and structured-output recovery.
- Dynamic candidate discovery and quality-first ranking.
- Guarded installation, promotion, rollback, quarantine, and protected LRU pruning.
- CLI, configuration, documentation, and deterministic tests.

### Machine orchestrator

- Port the public primitives into the canonical agent broker/manager where machine-specific lifecycle control lives.
- Migrate every production `local_llm`, `local_ai`, and `local_task` consumer.
- Remove arbitrary cross-role direct fallback.
- Add job-specific canaries, runtime learning, notification aggregation, and launch scheduling.

### Current incident closure

- Sara coach: distinguish successful generation rejected by safety rails from an unavailable model; use only coach-compatible fallbacks.
- Discord: treat repeated schema violations as incompatibility and change models rather than repeating the same failure.
- JJacked improvement pass: reject or normalize wrong top-level JSON types without throwing.
- Repair the independent AI Requests timeout and Atlas deploy failures at their actual orchestration boundaries.
- Verify a clean dry-run briefing and the next scheduled production run.

### Release

- Run each repository's `/release-prep` independently.
- Merge and deploy only through `/release-prod` after its entry gates pass.
- Update the public package, repository documentation, and jknapp.com article to match verified behavior.

## Acceptance

- Replacing a role incumbent with any contract-compatible installed model requires no job-code change.
- Schema/safety incompatibility triggers a different compatible model, not three identical retries or an outage label.
- No installed compatible model triggers autonomous guarded discovery, installation, local canaries, and selection.
- A failed challenger cannot displace the incumbent; rollback is automatic and protected.
- Disk pressure evicts unprotected, least-recently-used eligible models even if an upstream source has disappeared.
- Every model-backed scheduled job is inventoried, contract-bound, and covered by deterministic failure simulation.
- The daily brief reports accurate causes and contains no current model-related failure.
- Public Cheapskate exposes and documents the same core behavior with a green full suite.
