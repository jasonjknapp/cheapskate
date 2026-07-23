# Self-Healing Lifecycle Hardening (H1–H4)

> **Status:** SPEC-GATED, implementation-ready — approved via /spec-gate 2026-07-23
> (plan by primary session; adversarial review + spec authorship by Fable reviewer).
> Supersedes the "deferred findings" version of this file. Branch:
> `feature/model-self-healing` in the existing worktree
> `/Users/jason/dev/Personal/.worktrees/cheapskate/model-self-healing` (base: 5a8d9da).

## Goal (verbatim)

From Jason's resume prompt (2026-07-23):
> "Item 2 — Cheapskate /spec-gate H1-H4. Invoke the `spec-gate` SKILL … Cheapskate worktree:
> /Users/jason/dev/Personal/.worktrees/cheapskate/model-self-healing, branch feature/model-self-healing,
> HEAD 5a8d9da (last code commit 91259e7), unpushed, no PR. Deferred H1-H4 spec:
> docs/specs/self-healing-lifecycle-hardening.md in that worktree."

Standing decision: Jason chose Scope A for the shipped never_cloud/attribution release, then
PAUSE + /spec-gate the remaining hardening (H1–H4). Do NOT resume the Sol convergence loop —
this spec drives a FRESH implementation session.

## Context & findings (all verified against source at 5a8d9da)

**Reachability split:** `grep -rn "promote=" src/` → no results. No code in `src/` wires the
promotion lifecycle into `SelfHealingEngine.run()`; the public client passes only
`invoke`+`validate` (client.py:526-531). So **H1/H2 are LATENT** (correctness of a contract no
in-repo caller exercises yet — the future machine orchestrator will) while **H3/H4 are LIVE**
on the shipped public path.

- **H1 (LATENT, P1)** — promoted challenger not rolled back on first live failure.
  self_healing.py:336 `promote(...)` → :347-353 `model_promoted` → :354 post-promote
  `_try_candidates([candidate], …)`. On a `None` result the loop continues and :367 raises
  `NoCompatibleModel` without restoring the prior incumbent. `run()` (:219-231) has no
  `rollback` param. The registry-layer mechanism already exists: registry/registry.py:189
  `rollback()` and registry/currency.py:427 `rollback()` (snapshot-verified restore).
- **H2 (LATENT, P1)** — rollback candidates resolved by model-string only.
  resolve.py:250 reads the `rollback` string list; :265 resolves each via
  `resolve(model=…)`, which falls to `infer_backend` (:224, :51-62) when the string matches
  no live role entry — a former LM Studio incumbent `vendor/model` is mis-inferred as MLX,
  losing endpoint + size. The richer snapshot exists: promotion writes `rollback_configs`
  (registry/registry.py:168-169, `{model: {backend, endpoint, approx_gb, …}}`);
  currency.py:439 already consults it on the registry rollback path.
  **Placement constraint (reviewer finding):** the broker resolves model strings
  independently — broker/app.py:484 (`resolve(model=payload["model"])` pre-check) and
  backends/preflight.py:150 (`ensure_role` → `resolve(...)`, the actual dispatch) — and the
  client's per-candidate never_cloud guard calls `resolve(model=…)` directly
  (client.py:347-349). A fix confined to `role_candidates()` would leave the broker
  mis-dispatching and would violate resolve.py:262-264's documented invariant ("Match the
  broker's explicit-model resolution exactly"). **The fix must live in `resolve()` itself.**
- **H3 (LIVE, P1)** — `complete()` can serve a globally-quarantined incumbent and drop the
  eligible fallback. client.py:188 seeds `candidates = [(model, role)]` (`(None, role)` for a
  role request = live broker role-resolve); :193 extends with `role_candidates(role)[1:]`.
  `role_candidates()` drops quarantined entries (resolve.py:246, 255-257), so when the
  incumbent is quarantined, `[0]` is the fallback and `[1:]` drops it — while the
  `(None, role)` request resolves via `resolve()` (which never consults quarantine,
  resolve.py:160-232) straight back to the quarantined incumbent. The shipped `generate_json`
  role path does NOT have this bug (full list, concrete models — client.py:465-523), and
  neither does the router (router/task.py:353, 398-400).
- **H4 (LIVE, P1)** — explicit-model `generate_json(model=…)` skips the served-model
  provenance check. client.py:535-559 posts and parses with no identity check; the role path
  enforces `served != candidate.model → CheapskateUnavailable` at client.py:518-522. A
  backend-side fallback can satisfy an exact-model call with mis-attributed output.

## Design

**Recommendation: fix all four on one atomic branch, ordered H4 → H3 → H1 → H2** — they share
one invariant: *a result must be attributed to the model that actually served it, and a
known-bad model must never be served or stay promoted.* H3+H4 are the live-path priority;
H1+H2 complete the latent engine contract for the future orchestrator. Each phase is
independently testable and revertable.

### H4 — served-model gate on the explicit-model path (first: smallest, highest live value)
In the explicit-model branch (client.py:535-559), after `_post_chat` and before parsing,
apply the same gate the role path uses (client.py:518-522): when `candidate` is non-None,
`served = body.get("model")`; if `served != candidate`, raise
`CheapskateUnavailable(f"requested {candidate!r} but broker served {served!r}")`. A backend
that omits `model` (served None) fails closed — provenance is required, not assumed. The
mismatch raises `CheapskateUnavailable` (caught at :547, no repair nudge appended — a
provenance failure is not a schema failure). The degenerate `model=None, role=None` call is
unreachable past the broker (400) and under never_cloud is refused earlier (:398-415); the
gate simply does not apply when `candidate` is None.

### H3 — align `complete()`'s role path with `generate_json` and the router
**Wire-contract decision (binding):** for `complete(role=…, model=None)`, drive the candidate
list off the FULL quarantine-aware `role_candidates(role)` list, invoking each by concrete
model (`(spec.model, None)`), and **DROP the `(None, role)` live-broker-role seed.**
Client-side, quarantine-aware resolution becomes authoritative for every role path of the
Python client — matching the two already-shipped precedents (`generate_json` role path,
client.py:465-523; router `_run_local`, router/task.py:353, 398-400). The broker's
`role:<name>` wire capability is NOT removed: `resolve()` still decodes it
(resolve.py:179-180) and `/v1/models` still advertises role ids (broker/app.py:755) for
external OpenAI-compat clients. Keep the `(model, role)` seed shape for all non-role calls
(including the both-given quirk where `_post_chat` prefers role — untouched). Preserve the
per-candidate never_cloud guard (client.py:204-211) and the
`LocalUnavailable → CheapskateUnavailable` translation (client.py:192-197). When
`role_candidates()` returns an empty list (e.g. every entry quarantined), raise
`CheapskateUnavailable` with a message naming the role and that all candidates are
quarantined/unavailable — never a bare "None".

### H1 — engine `rollback` callback (mandatory with `promote`)
Add `rollback: Callable[[Candidate, JobContract], bool] | None = None` to `run()`
(symmetric with `promote`, self_healing.py:230). Semantics:
- **Trigger:** whenever the post-promote `_try_candidates` (:354) returns no result — for
  ANY reason, including deadline exhaustion. Rationale: the challenger is unproven live and
  the deposed incumbent was proven; "a failed challenger cannot displace the incumbent"
  (docs/plans/global-model-self-healing.md:80). On trigger: call
  `rollback(candidate, contract)` and fire a `model_rollback` notification (include job_id,
  model, backend) BEFORE continuing the discovered loop / raising.
- **Callback failure:** a raising rollback is recorded via `_adapter_failure`
  (self_healing.py:208-217); a False return is recorded as a failure entry. The engine never
  crashes on a broken rollback adapter, and still raises `NoCompatibleModel` at the end —
  rollback is never a silent success path.
- **Guard:** `promote is not None and rollback is None` → refuse discovery with a SAFETY
  failure entry, exactly like the existing `discover`/`install` without
  `fit`/`evaluate`/`promote` guard (self_healing.py:246-252). Callers that can promote MUST
  be able to undo it.
- The engine stays adapter-driven (no registry import); the callback body already exists at
  registry/currency.py:427 for the future orchestrator to inject.
- **Known churn (enumerate, don't discover mid-flight):** tests/test_self_healing.py:82-94
  and the parametrized adapter test at :120-147 pass `promote=` without `rollback=` — both
  gain a rollback stub (`lambda c, k: True`).

### H2 — rollback snapshot resolution inside `resolve()` (NOT role_candidates)
In `resolve()` (resolve.py:160-232), after the live role-entry model scan (:211-222) and
before the `infer_backend` fallback (:224): scan the role table for an entry whose
`rollback_configs` (a dict) contains the requested model with a dict snapshot carrying a
`backend`; if found, build the `BackendSpec` from the snapshot (model, backend,
`endpoint or default_endpoint(backend, config)`, approx_gb, role=<owning role>, quant).
Precedence: a live incumbent match always wins over a snapshot (stale metadata never shadows
live state); first matching role in table order wins (degenerate cross-role duplicates are
deterministic). No snapshot → current string inference, unchanged (back-compat with older
registries / hand-edited entries). This single fix point is inherited by `role_candidates`
(:265), the client's never_cloud guards (client.py:347-349), the broker pre-check
(broker/app.py:484), and the broker dispatch (preflight.py:150) — no divergent resolution,
honoring resolve.py:262-264.
**Snapshot trust:** no bespoke validation. The resolved spec flows through the existing
fail-closed gates — client backend-allowlist + loopback (client.py:332-339), broker
never_cloud pre-check (app.py:494), and the broker's post-prepare re-verification of the
ACTUAL base URL (app.py:573-585) — so a poisoned snapshot cannot smuggle a nonlocal endpoint
into a never_cloud route. Under cloud_allowed it carries exactly the trust of a role
`endpoint` field today (same local-state trust class).

## Phases (ordered; each AC gates the next)

Test cmd: `PYTHONPATH=src /Users/jason/dev/Personal/cheapskate/.venv/bin/python -m pytest -q -p no:cacheprovider`
Lint: `/Users/jason/dev/Personal/cheapskate/.venv/bin/ruff check src tests`

1. **H4 — explicit-model served-model gate.**
   Update the `_chat_body` fixture usage in `tests/test_client.py` (the helper is at
   tests/test_client.py:50 — NOT conftest.py) so explicit-model stubs serve the REQUESTED
   model. Known churn set: `test_generate_json_parses_object`,
   `test_generate_json_repairs_then_succeeds`, `test_generate_json_exhausts_retries_and_degrades`
   (passes today for the wrong reason once the gate lands — fix its fixture anyway),
   `test_generate_json_validates_pydantic_schema`,
   `test_generate_json_repairs_valid_json_with_wrong_schema_root`. No assertion may be
   weakened to make a test pass. Add: positive (served==requested), negative
   (served!=requested → CheapskateUnavailable), and served-None → CheapskateUnavailable tests.
   **AC:** new tests pass; full suite green; Ruff clean; zero role-path test changes in this phase.
2. **H3 — `complete()` client-resolved candidates.**
   Known churn set (the only `role:` wire assertions on the complete() path):
   tests/test_client.py:70, :88-89, :107-108 — update to the concrete-model shape.
   Add: (a) incumbent quarantined → complete() serves the FALLBACK, never the incumbent;
   (b) no quarantine → still serves the incumbent (no regression); (c) fully-quarantined
   role → CheapskateUnavailable with the role named.
   **AC:** (a)–(c) pass; updated wire-shape tests pass; full suite green + Ruff clean.
3. **H1 — engine `rollback` callback.**
   Add rollback stubs to tests/test_self_healing.py:82-94 and :120-147.
   Add: promote→invoke-fails → rollback called once with the promoted candidate,
   `model_rollback` notified, `NoCompatibleModel` still raised; promote-without-rollback →
   discovery refused with a SAFETY failure (guard test); rollback-callback-raises → recorded
   via adapter-failure, engine still raises cleanly.
   **AC:** all new + churned engine tests pass; full suite green + Ruff clean.
4. **H2 — snapshot resolution in `resolve()`.**
   Add: (a) registry role entry with an lmstudio `rollback_configs` snapshot (loopback
   endpoint) → `resolve(model=…)` returns backend=lmstudio + stored endpoint + stored size,
   NOT MLX inference; (b) same model then appears through `role_candidates()` with the
   snapshot spec; (c) live-incumbent-wins precedence test; (d) no snapshot → string-path
   back-compat; (e) never_cloud + snapshot with a NONLOCAL endpoint → route refused by the
   existing gates (client-side test).
   **AC:** (a)–(e) pass; full suite green + Ruff clean.
5. **Release.**
   **AC:** full suite green + Ruff clean, then `/release-prep` (runs the full 2-clean
   adversarial review gate inline) → PR → Jason's explicit go → `/release-prod`.

## Risks & mitigations

- **R1 (H3 wire contract):** no in-repo consumer or test relies on complete() sending
  `role:<name>` (verified: the only assertions are the three churned tests above; broker
  tests exercise `role:` by posting it themselves). Client-vs-broker config divergence risk
  is identical to the already-shipped generate_json role path (both read the same
  state_dir registry). Broker `role:` support is unchanged for external clients.
- **R2 (H1 caller obligation):** mandatory-with-promote breaks no in-repo caller (none wire
  promote). Cross-repo note for the ~/.agents machine orchestrator (Item 3): when it wires
  `promote`, it MUST wire `rollback` (registry/currency.py:427 is the intended body). Record
  in that effort's backlog; not a blocker here.
- **R3 (H2 snapshot trust):** handled structurally by fix placement — the snapshot-resolved
  spec passes through every existing never_cloud gate (see Design). No new validation code.
- **R4 (test churn):** every churned test is enumerated per phase above; the rule is
  fixtures serve honest identities — never weaken an assertion.
- **R5 (deadline-triggered rollback):** rolling back a challenger on deadline exhaustion may
  discard a viable model; accepted deliberately — the incumbent was proven, the challenger
  was not, and the next currency pass can re-nominate it.

## Out of scope

- Wiring the promotion lifecycle (discover/install/evaluate/promote/rollback) into any
  public caller — the machine-orchestrator effort (Item 3, ~/.agents) owns that; this spec
  makes the LIBRARY correct for when it is wired.
- `complete()`'s explicit-model provenance: stays advisory BY DECISION — complete() exposes
  raw `served_model` for callers (client.py:232-237) and its production consumer already
  fails closed on it (router/task.py:407-412). Only generate_json's explicit path gains the
  hard gate (H4).
- Broker-side served-model verification / never-local role policy (Agent A1/A2 follow-up,
  different repo).
- Any change to shipped R5-1 (env-proxy) / R5-4 (typed-config) behavior.
- Resuming the Sol convergence loop.
- Renumbering this spec: cheapskate has no spec namespace in spec_alloc.py; this named file
  is the canonical spec (do not invent a numbered id).

## STOP points (Jason's explicit in-the-moment approval required)

- No `git push`, PR creation, or merge outside `/release-prep` → `/release-prod`.
- `/release-prep` exit (staging approval) and `/release-prod` entry/merge are Jason's calls.
- No paid CI builds (none expected for this repo; if any paid pipeline appears, stop and ask).
- Work stays on `feature/model-self-healing` in the existing worktree; never fix on main.

## § Implementation Prompt

Implement **Self-Healing Lifecycle Hardening (H1–H4)** per
`/Users/jason/dev/Personal/.worktrees/cheapskate/model-self-healing/docs/specs/self-healing-lifecycle-hardening.md`.
Read the spec IN FULL first — it is binding, including the H2 fix placement (inside
`resolve()`, NOT `role_candidates()`), the H3 wire-contract decision (drop the `(None, role)`
seed; concrete-model candidates from the full quarantine-aware `role_candidates()` list), the
H1 mandatory-with-promote rollback guard, and the enumerated test-churn sets. Work on branch
`feature/model-self-healing` in the existing worktree
`/Users/jason/dev/Personal/.worktrees/cheapskate/model-self-healing` (base 5a8d9da; do not
create a new worktree; claim the repo lock if prompted). Execute phases in order
(H4 → H3 → H1 → H2 → Release); verify each phase's acceptance check with
`PYTHONPATH=src /Users/jason/dev/Personal/cheapskate/.venv/bin/python -m pytest -q -p no:cacheprovider`
and `/Users/jason/dev/Personal/cheapskate/.venv/bin/ruff check src tests` before starting the
next. Never weaken a test assertion to pass a phase. When complete, run `/release-prep` —
do not push, open a PR, or merge outside it.
