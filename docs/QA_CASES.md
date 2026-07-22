# QA cases

## Model self-healing

- A schema-invalid but responsive model is classified as a schema incompatibility, not an outage.
- A job retries one repair on its current model, then switches to a compatible installed model.
- Incompatibility is scoped to the job and model; the same model remains eligible for other jobs.
- If installed candidates are exhausted, the engine installs and tries the highest-ranked compatible discovery candidate.
- Discovery is global rather than publisher-allowlisted, with strong release-recency weighting.
- Promotion remains eval-gated; discovery popularity never overrides a failed local quality floor.
- Storage pressure prunes eligible, unprotected models in least-recently-used order without requiring a live upstream source.
- Incumbent, fallback, rollback, pinned, shared, and loaded models are never automatically deleted.
