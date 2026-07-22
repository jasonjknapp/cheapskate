# QA cases

## Model self-healing

- A schema-invalid but responsive model is classified as a schema incompatibility, not an outage.
- A job retries one repair on its current model, then switches to a compatible installed model.
- Incompatibility is scoped to the job and model; the same model remains eligible for other jobs.
- A `never_cloud` job requires positively local endpoint provenance; a remote URL remains rejected even when its backend label says Ollama or MLX.
- A `never_cloud` job also requires a loopback broker URL for role and explicit-model calls; a remote broker is rejected before any HTTP request.
- If installed candidates are exhausted, the engine installs and tries the highest-ranked compatible discovery candidate.
- Discovery is global rather than publisher-allowlisted, with strong release-recency weighting.
- Promotion remains eval-gated; discovery popularity never overrides a failed local quality floor.
- Storage pressure prunes eligible, unprotected models in least-recently-used order without requiring a live upstream source.
- LRU sorting accepts numeric, ISO, missing, and malformed timestamps deterministically; missing or malformed usage is oldest.
- Incumbent, fallback, rollback, pinned, shared, and loaded models are never automatically deleted.
- Ollama installation checks match the exact normalized tag (`foo` means `foo:latest`, never another `foo:<tag>`).
- Rollback requires a verified installed target and restores the full prior serving snapshot across backend changes.
- Every shipped default has a backend consistent with its model identifier; the classification default resolves as the installed Ollama tag, not an MLX repository.
