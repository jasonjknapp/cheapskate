# SPDX-License-Identifier: Apache-2.0
"""Capability-aware recovery shared by scheduled jobs and interactive clients.

The engine is deliberately adapter-driven: it owns ordering, bounded repair,
job-scoped compatibility, autonomous install, and notifications while callers
retain their backend-specific invocation, validation, and installation code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .contracts import FailureKind, JobContract, classify_failure


@dataclass(frozen=True, slots=True)
class Candidate:
    model: str
    backend: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    discovery_score: float = 0.0
    latency_ms: float | None = None
    installed: bool = True


@dataclass(frozen=True, slots=True)
class RunResult:
    output: Any
    model: str
    backend: str
    attempts: int
    recovered: bool


class NoCompatibleModel(RuntimeError):
    """Every eligible installed/discovered candidate failed the job contract."""


class CompatibilityStore:
    """Job-scoped model failures, optionally persisted as atomic JSON."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        if path is not None:
            try:
                loaded = json.loads(path.read_text())
                if isinstance(loaded, dict):
                    self._data = loaded
            except (OSError, ValueError):
                pass

    def block(self, job_id: str, model: str, kind: FailureKind, detail: str) -> None:
        self._data.setdefault(job_id, {})[model] = {
            "kind": kind.value,
            "detail": detail[:300],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def mark_compatible(self, job_id: str, model: str) -> None:
        models = self._data.get(job_id, {})
        if model in models:
            del models[model]
            self._save()

    def is_blocked(self, job_id: str, model: str) -> bool:
        return model in self._data.get(job_id, {})

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)


NotifyFn = Callable[[dict[str, Any]], None]
InvokeFn = Callable[[Candidate, str | None], Any]
ValidateFn = Callable[[Any], tuple[bool, str]]


class SelfHealingEngine:
    def __init__(
        self,
        *,
        compatibility: CompatibilityStore | None = None,
        notify: NotifyFn | None = None,
    ):
        self.compatibility = compatibility or CompatibilityStore()
        self.notify = notify or (lambda event: None)

    def run(
        self,
        contract: JobContract,
        installed: Iterable[Candidate],
        *,
        invoke: InvokeFn,
        validate: ValidateFn,
        discover: Callable[[JobContract], Iterable[Candidate]] | None = None,
        install: Callable[[Candidate], bool] | None = None,
    ) -> RunResult:
        attempts = 0
        failures: list[dict[str, str]] = []
        candidates = self._eligible(contract, installed)
        result, attempts = self._try_candidates(
            contract, candidates, invoke, validate, attempts, failures
        )
        if result is not None:
            return result

        if discover is not None and install is not None:
            discovered = self._eligible(contract, discover(contract), include_blocked=False)
            for candidate in discovered:
                if candidate.model in {c.model for c in candidates}:
                    continue
                if not install(candidate):
                    failures.append({"model": candidate.model, "kind": "install_failed"})
                    continue
                self.notify({
                    "event": "model_installed",
                    "job_id": contract.job_id,
                    "model": candidate.model,
                    "backend": candidate.backend,
                })
                result, attempts = self._try_candidates(
                    contract, [candidate], invoke, validate, attempts, failures
                )
                if result is not None:
                    return result

        event = {
            "event": "model_recovery_failed",
            "job_id": contract.job_id,
            "role": contract.role,
            "failures": failures,
        }
        self.notify(event)
        raise NoCompatibleModel(
            f"no compatible model satisfied {contract.job_id!r}; failures={failures}"
        )

    def _eligible(
        self,
        contract: JobContract,
        candidates: Iterable[Candidate],
        *,
        include_blocked: bool = False,
    ) -> list[Candidate]:
        eligible = [
            c for c in candidates
            if contract.accepts_capabilities(c.capabilities)
            and (include_blocked or not self.compatibility.is_blocked(contract.job_id, c.model))
        ]
        # Quality/discovery rank is primary. Latency only breaks an equal-quality tie.
        return sorted(
            eligible,
            key=lambda c: (c.discovery_score, -(c.latency_ms or 0)),
            reverse=True,
        )

    def _try_candidates(
        self,
        contract: JobContract,
        candidates: Iterable[Candidate],
        invoke: InvokeFn,
        validate: ValidateFn,
        attempts: int,
        failures: list[dict[str, str]],
    ) -> tuple[RunResult | None, int]:
        candidate_list = list(candidates)
        first_model = candidate_list[0].model if candidate_list else None
        for candidate in candidate_list:
            feedback: str | None = None
            final_kind = FailureKind.UNKNOWN
            final_detail = "unknown failure"
            for _ in range(contract.repair_attempts + 1):
                attempts += 1
                try:
                    output = invoke(candidate, feedback)
                    valid, detail = validate(output)
                    if valid:
                        self.compatibility.mark_compatible(contract.job_id, candidate.model)
                        recovered = attempts > 1 or candidate.model != first_model
                        if recovered:
                            self.notify({
                                "event": "model_failover_succeeded",
                                "job_id": contract.job_id,
                                "model": candidate.model,
                                "backend": candidate.backend,
                                "attempts": attempts,
                            })
                        return RunResult(
                            output=output,
                            model=candidate.model,
                            backend=candidate.backend,
                            attempts=attempts,
                            recovered=recovered,
                        ), attempts
                    final_kind = classify_failure(detail)
                    if final_kind is FailureKind.UNKNOWN:
                        final_kind = (
                            FailureKind.SCHEMA
                            if contract.output_mode == "json"
                            else FailureKind.QUALITY
                        )
                    final_detail = detail
                    feedback = detail
                except Exception as exc:  # noqa: BLE001 - failure is classified for recovery
                    final_kind = classify_failure(exc)
                    final_detail = f"{type(exc).__name__}: {exc}"
                    feedback = final_detail
            if final_kind in {
                FailureKind.SCHEMA,
                FailureKind.SAFETY,
                FailureKind.QUALITY,
                FailureKind.INCOMPATIBLE,
            }:
                self.compatibility.block(
                    contract.job_id, candidate.model, final_kind, final_detail
                )
            failures.append({
                "model": candidate.model,
                "kind": final_kind.value,
                "detail": final_detail[:300],
            })
        return None, attempts


def lru_prune_plan(
    managed: dict[str, dict[str, Any]],
    *,
    protected: set[str],
    need_gb: float,
) -> list[dict[str, Any]]:
    """Oldest eligible managed models to remove until ``need_gb`` is freed.

    The caller owns deletion and should re-check the protected set immediately
    before acting. Source availability is deliberately not a gate: discontinued
    models must not become permanent disk occupants.
    """

    eligible = []
    for model, meta in managed.items():
        if model in protected or meta.get("prune") == "never":
            continue
        size = float(meta.get("size_gb") or meta.get("approx_gb") or 0)
        if size <= 0:
            continue
        eligible.append({"model": model, "size_gb": size, **meta})
    eligible.sort(key=lambda item: (item.get("last_used") or "", item["model"]))
    plan: list[dict[str, Any]] = []
    freed = 0.0
    for item in eligible:
        if freed >= need_gb:
            break
        plan.append(item)
        freed += item["size_gb"]
    return plan
