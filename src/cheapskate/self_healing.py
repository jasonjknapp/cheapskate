# SPDX-License-Identifier: Apache-2.0
"""Capability-aware recovery shared by scheduled jobs and interactive clients.

The engine is deliberately adapter-driven: it owns ordering, bounded repair,
job-scoped compatibility, autonomous install, and notifications while callers
retain their backend-specific invocation, validation, and installation code.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    local: bool | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    output: Any
    model: str
    backend: str
    attempts: int
    recovered: bool
    notification_failures: tuple[dict[str, str], ...] = ()


class NoCompatibleModel(RuntimeError):
    """Every eligible installed/discovered candidate failed the job contract."""


class CompatibilityStore:
    """Expiring, job-scoped failures persisted with flocked atomic writes."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_s: float = 7 * 24 * 60 * 60,
        now: Callable[[], datetime] | None = None,
    ):
        self.path = path
        self.ttl_s = ttl_s
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._data: dict[str, dict[str, dict[str, Any]]] = {}
        self._reload()

    @contextmanager
    def _locked(self):
        if self.path is None:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._reload()
            yield

    def _reload(self) -> None:
        if self.path is None:
            return
        try:
            loaded = json.loads(self.path.read_text())
            self._data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self._data = {}

    def block(self, job_id: str, model: str, kind: FailureKind, detail: str) -> None:
        with self._locked():
            now = self._now()
            self._data.setdefault(job_id, {})[model] = {
                "kind": kind.value,
                "detail": detail[:300],
                "at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=self.ttl_s)).isoformat(),
            }
            self._save()

    def mark_compatible(self, job_id: str, model: str) -> None:
        with self._locked():
            models = self._data.get(job_id, {})
            if model in models:
                del models[model]
                if not models:
                    self._data.pop(job_id, None)
                self._save()

    def is_blocked(self, job_id: str, model: str) -> bool:
        with self._locked():
            entry = self._data.get(job_id, {}).get(model)
            if not entry:
                return False
            expires_at = entry.get("expires_at")
            try:
                expires = datetime.fromisoformat(str(expires_at))
            except (TypeError, ValueError):
                try:
                    blocked_at = datetime.fromisoformat(str(entry["at"]))
                    expires = blocked_at + timedelta(seconds=self.ttl_s)
                except (KeyError, TypeError, ValueError):
                    expires = self._now()
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires > self._now():
                return True
            del self._data[job_id][model]
            if not self._data[job_id]:
                del self._data[job_id]
            self._save()
            return False

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.path)


NotifyFn = Callable[[dict[str, Any]], None]
InvokeFn = Callable[[Candidate, str | None], Any]
ValidateFn = Callable[[Any], tuple[bool, str] | tuple[bool, str, float]]
FitFn = Callable[[Candidate, JobContract], tuple[bool, str]]
EvaluateFn = Callable[[Candidate, JobContract], tuple[bool, str, float]]
PromoteFn = Callable[[Candidate, JobContract], bool]
RollbackFn = Callable[[Candidate, JobContract], bool]


class SelfHealingEngine:
    def __init__(
        self,
        *,
        compatibility: CompatibilityStore | None = None,
        notify: NotifyFn | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.compatibility = compatibility or CompatibilityStore()
        self.notify = notify or (lambda event: None)
        self.monotonic = monotonic or time.monotonic
        self.notification_failures: list[dict[str, str]] = []

    def _notify(self, event: dict[str, Any]) -> None:
        """Deliver a notification without making observability a serving dependency."""

        try:
            self.notify(event)
        except Exception as exc:  # noqa: BLE001 - recovery must survive a broken sink
            self.notification_failures.append({
                "event": str(event.get("event") or "unknown"),
                "job_id": str(event.get("job_id") or ""),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })

    def _deadline_exhausted(
        self,
        contract: JobContract,
        started_at: float,
        failures: list[dict[str, str]],
        model: str,
    ) -> bool:
        """True once the job deadline plus allowed late window has elapsed.

        The engine checks this boundary before starting each adapter operation and
        before accepting its result. Adapters still own cancellation/timeouts for
        an individual blocking call; no thread or process is leaked to fake that.
        """

        if contract.deadline_s is None:
            return False
        budget_s = contract.deadline_s + contract.bounded_late_s
        if self.monotonic() <= started_at + budget_s:
            return False
        detail = (
            f"contract deadline exhausted after {budget_s:g}s "
            f"({contract.deadline_s:g}s deadline + {contract.bounded_late_s:g}s late window)"
        )
        if not any(
            failure.get("kind") == FailureKind.TIMEOUT.value
            and str(failure.get("detail", "")).startswith("contract deadline exhausted")
            for failure in failures
        ):
            failures.append({
                "model": model,
                "kind": FailureKind.TIMEOUT.value,
                "detail": detail,
            })
        return True

    def _rollback(
        self,
        candidate: Candidate,
        contract: JobContract,
        rollback: RollbackFn | None,
        failures: list[dict[str, str]],
    ) -> None:
        """Restore the prior incumbent after a promoted challenger failed to serve.

        A raising callback is recorded via ``_adapter_failure``; a False return is
        recorded as a SAFETY failure entry. The rollback is never a silent success
        path — the engine still raises ``NoCompatibleModel`` at the end — and a
        broken rollback adapter never crashes the engine. A ``model_rollback``
        notification always fires so the restoration is observable.
        """
        if rollback is None:
            return
        try:
            restored = rollback(candidate, contract)
        except Exception as exc:  # noqa: BLE001 - a broken adapter must not crash recovery
            self._adapter_failure(failures, candidate.model, "rollback", exc)
        else:
            if not restored:
                failures.append({
                    "model": candidate.model,
                    "kind": FailureKind.SAFETY.value,
                    "detail": "rollback gate did not restore the prior incumbent",
                })
        self._notify({
            "event": "model_rollback",
            "job_id": contract.job_id,
            "model": candidate.model,
            "backend": candidate.backend,
        })

    @staticmethod
    def _adapter_failure(
        failures: list[dict[str, str]], model: str, stage: str, exc: BaseException
    ) -> None:
        kind = classify_failure(exc)
        failures.append({
            "model": model,
            "kind": kind.value,
            "detail": f"{stage} adapter failed: {type(exc).__name__}: {exc}"[:300],
        })

    def run(
        self,
        contract: JobContract,
        installed: Iterable[Candidate],
        *,
        invoke: InvokeFn,
        validate: ValidateFn,
        discover: Callable[[JobContract], Iterable[Candidate]] | None = None,
        install: Callable[[Candidate], bool] | None = None,
        fit: FitFn | None = None,
        evaluate: EvaluateFn | None = None,
        promote: PromoteFn | None = None,
        rollback: RollbackFn | None = None,
    ) -> RunResult:
        self.notification_failures = []
        started_at = self.monotonic()
        attempts = 0
        failures: list[dict[str, str]] = []
        candidates = self._eligible(contract, installed, require_installed=True)
        result, attempts = self._try_candidates(
            contract, candidates, invoke, validate, attempts, failures, started_at
        )
        if result is not None:
            return result

        deadline_exhausted = self._deadline_exhausted(
            contract, started_at, failures, "discovery"
        )
        if not deadline_exhausted and discover is not None and install is not None:
            if fit is None or evaluate is None or promote is None:
                failures.append({
                    "model": "discovery",
                    "kind": FailureKind.SAFETY.value,
                    "detail": "discovery requires fit, eval/canary, and promotion gates",
                })
                discovered = []
            elif rollback is None:
                # A caller that can promote a challenger MUST be able to undo it:
                # a failed promotion that stays incumbent is worse than no recovery.
                failures.append({
                    "model": "discovery",
                    "kind": FailureKind.SAFETY.value,
                    "detail": "promotion requires a rollback gate to undo a failed challenger",
                })
                discovered = []
            else:
                try:
                    discovered_raw = discover(contract)
                except Exception as exc:  # noqa: BLE001 - isolate optional adapters
                    self._adapter_failure(failures, "discovery", "discover", exc)
                    discovered_raw = []
                if self._deadline_exhausted(contract, started_at, failures, "discovery"):
                    discovered = []
                else:
                    discovered = self._eligible(
                        contract,
                        discovered_raw,
                        include_blocked=False,
                        require_installed=False,
                        rank_discovery=True,
                    )
            for candidate in discovered:
                if candidate.model in {c.model for c in candidates}:
                    continue
                if self._deadline_exhausted(
                    contract, started_at, failures, candidate.model
                ):
                    break
                try:
                    fits, fit_detail = fit(candidate, contract)
                except Exception as exc:  # noqa: BLE001
                    self._adapter_failure(failures, candidate.model, "fit", exc)
                    continue
                if self._deadline_exhausted(
                    contract, started_at, failures, candidate.model
                ):
                    break
                if not fits:
                    failures.append({
                        "model": candidate.model,
                        "kind": FailureKind.INCOMPATIBLE.value,
                        "detail": f"fit gate: {fit_detail}"[:300],
                    })
                    continue
                try:
                    installed_ok = install(candidate)
                except Exception as exc:  # noqa: BLE001
                    self._adapter_failure(failures, candidate.model, "install", exc)
                    continue
                if not installed_ok:
                    failures.append({"model": candidate.model, "kind": "install_failed"})
                    continue
                self._notify({
                    "event": "model_installed",
                    "job_id": contract.job_id,
                    "model": candidate.model,
                    "backend": candidate.backend,
                })
                if self._deadline_exhausted(
                    contract, started_at, failures, candidate.model
                ):
                    break
                try:
                    eval_ok, eval_detail, quality = evaluate(candidate, contract)
                except Exception as exc:  # noqa: BLE001
                    self._adapter_failure(failures, candidate.model, "evaluate", exc)
                    continue
                if self._deadline_exhausted(
                    contract, started_at, failures, candidate.model
                ):
                    break
                if not eval_ok or quality < contract.quality_floor:
                    detail = (
                        eval_detail
                        if not eval_ok
                        else f"quality score {quality:.3f} below floor {contract.quality_floor:.3f}"
                    )
                    self.compatibility.block(
                        contract.job_id, candidate.model, FailureKind.QUALITY, detail
                    )
                    failures.append({
                        "model": candidate.model,
                        "kind": FailureKind.QUALITY.value,
                        "detail": detail[:300],
                    })
                    continue
                try:
                    promoted = promote(candidate, contract)
                except Exception as exc:  # noqa: BLE001
                    self._adapter_failure(failures, candidate.model, "promote", exc)
                    continue
                if not promoted:
                    failures.append({
                        "model": candidate.model,
                        "kind": FailureKind.SAFETY.value,
                        "detail": "promotion gate refused candidate",
                    })
                    continue
                self._notify({
                    "event": "model_promoted",
                    "job_id": contract.job_id,
                    "model": candidate.model,
                    "backend": candidate.backend,
                    "quality": quality,
                })
                result, attempts = self._try_candidates(
                    contract, [candidate], invoke, validate, attempts, failures, started_at
                )
                if result is not None:
                    return result
                # The challenger was promoted but could not serve the live job (for
                # ANY reason, including deadline exhaustion). It is unproven and has
                # displaced a proven incumbent — restore the incumbent before we
                # continue the discovered loop or raise. (rollback is guaranteed
                # non-None here by the guard above.)
                self._rollback(candidate, contract, rollback, failures)

        event = {
            "event": "model_recovery_failed",
            "job_id": contract.job_id,
            "role": contract.role,
            "failures": failures,
        }
        self._notify(event)
        raise NoCompatibleModel(
            f"no compatible model satisfied {contract.job_id!r}; failures={failures}"
        )

    def _eligible(
        self,
        contract: JobContract,
        candidates: Iterable[Candidate],
        *,
        include_blocked: bool = False,
        require_installed: bool = True,
        rank_discovery: bool = False,
    ) -> list[Candidate]:
        eligible = [
            c for c in candidates
            if (c.installed or not require_installed)
            and contract.accepts_capabilities(c.capabilities)
            and not (contract.privacy == "never_cloud" and c.local is not True)
            and (include_blocked or not self.compatibility.is_blocked(contract.job_id, c.model))
        ]
        # Installed order is contractual: incumbent, fallback, retained rollback.
        # Discovery metadata only orders which challengers reach the local eval
        # gate first; latency never displaces a proven incumbent.
        if not rank_discovery:
            return eligible
        return sorted(eligible, key=lambda c: c.discovery_score, reverse=True)

    def _try_candidates(
        self,
        contract: JobContract,
        candidates: Iterable[Candidate],
        invoke: InvokeFn,
        validate: ValidateFn,
        attempts: int,
        failures: list[dict[str, str]],
        started_at: float,
    ) -> tuple[RunResult | None, int]:
        candidate_list = list(candidates)
        first_model = candidate_list[0].model if candidate_list else None
        for candidate in candidate_list:
            feedback: str | None = None
            final_kind = FailureKind.UNKNOWN
            final_detail = "unknown failure"
            for _ in range(contract.repair_attempts + 1):
                if self._deadline_exhausted(
                    contract, started_at, failures, candidate.model
                ):
                    return None, attempts
                attempts += 1
                try:
                    output = invoke(candidate, feedback)
                    if self._deadline_exhausted(
                        contract, started_at, failures, candidate.model
                    ):
                        return None, attempts
                    assessment = validate(output)
                    valid, detail = assessment[:2]
                    quality = assessment[2] if len(assessment) > 2 else (1.0 if valid else 0.0)
                    if valid and quality >= contract.quality_floor:
                        self.compatibility.mark_compatible(contract.job_id, candidate.model)
                        recovered = attempts > 1 or candidate.model != first_model
                        if recovered:
                            self._notify({
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
                            notification_failures=tuple(self.notification_failures),
                        ), attempts
                    if valid:
                        detail = (
                            f"quality score {quality:.3f} below floor "
                            f"{contract.quality_floor:.3f}"
                        )
                        valid = False
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
        if any(meta.get(flag) for flag in (
            "loaded",
            "in_use",
            "shared",
            "pinned",
            "active_challenger",
            "incumbent",
            "fallback",
            "rollback",
        )):
            continue
        try:
            size = float(meta.get("size_gb") or meta.get("approx_gb") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(size) or size <= 0:
            continue
        # State metadata is untrusted bookkeeping. It must not replace the map's
        # canonical model identity or the positive numeric size validated above.
        eligible.append({**meta, "model": model, "size_gb": size})
    def last_used_key(item: dict[str, Any]) -> tuple[float, str]:
        value = item.get("last_used")
        if isinstance(value, bool):
            timestamp = 0.0
        elif isinstance(value, (int, float)):
            timestamp = float(value) if math.isfinite(float(value)) else 0.0
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.timestamp()
            except (ValueError, OverflowError):
                timestamp = 0.0
        else:
            timestamp = 0.0
        return timestamp, item["model"]

    eligible.sort(key=last_used_key)
    plan: list[dict[str, Any]] = []
    freed = 0.0
    for item in eligible:
        if freed >= need_gb:
            break
        plan.append(item)
        freed += item["size_gb"]
    return plan
