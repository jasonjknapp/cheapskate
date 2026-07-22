# Code Review Style Guide

Rules for external code-review bots when reviewing this repository:

- Prefer small standard-library primitives and adapter injection over framework dependencies.
- Model ids belong in registries and fleet policy, not job definitions.
- Quality is the promotion authority; latency is only a tie-breaker after host-fit checks.
- Failure records are scoped to `(job_id, model)` unless a deterministic fleet-wide fault is proven.
- Destructive cleanup must preserve incumbent, fallback, rollback, pinned, shared, and loaded models.
