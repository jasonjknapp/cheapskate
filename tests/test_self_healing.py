# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from cheapskate.contracts import FailureKind, JobContract, classify_failure
from cheapskate.self_healing import Candidate, SelfHealingEngine, lru_prune_plan


def test_failure_taxonomy_does_not_call_bad_schema_an_outage():
    assert classify_failure(ValueError("response did not match schema")) is FailureKind.SCHEMA
    assert classify_failure(TimeoutError("slow")) is FailureKind.TIMEOUT
    assert classify_failure(RuntimeError("safety rail rejected output")) is FailureKind.SAFETY


def test_failover_repairs_once_then_uses_compatible_installed_model():
    calls: list[tuple[str, str | None]] = []
    notices: list[dict] = []
    contract = JobContract(
        job_id="discord.digest",
        role="classification",
        output_mode="json",
        repair_attempts=1,
    )

    def invoke(model: Candidate, feedback: str | None):
        calls.append((model.model, feedback))
        if model.model == "fast-but-wrong":
            return []
        return {"themes": ["shipping"]}

    def validate(value):
        return (isinstance(value, dict), "root must be an object")

    engine = SelfHealingEngine(notify=notices.append)
    result = engine.run(
        contract,
        [
            Candidate("fast-but-wrong", "mlx", frozenset({"json"})),
            Candidate("reliable", "ollama", frozenset({"json"})),
        ],
        invoke=invoke,
        validate=validate,
    )

    assert result.output == {"themes": ["shipping"]}
    assert result.model == "reliable"
    assert calls == [
        ("fast-but-wrong", None),
        ("fast-but-wrong", "root must be an object"),
        ("reliable", None),
    ]
    assert engine.compatibility.is_blocked("discord.digest", "fast-but-wrong")
    assert not engine.compatibility.is_blocked("other.job", "fast-but-wrong")
    assert notices[-1]["event"] == "model_failover_succeeded"


def test_engine_installs_best_ranked_candidate_when_installed_fleet_exhausted():
    notices: list[dict] = []
    installed: list[str] = []
    contract = JobContract(job_id="sara.coach", role="reasoning", required_capabilities={"reasoning"})
    discovered = [
        Candidate("new-best", "mlx", frozenset({"reasoning"}), discovery_score=0.93),
        Candidate("older", "mlx", frozenset({"reasoning"}), discovery_score=0.88),
    ]

    def install(candidate):
        installed.append(candidate.model)
        return True

    engine = SelfHealingEngine(notify=notices.append)
    result = engine.run(
        contract,
        [Candidate("broken", "mlx", frozenset({"reasoning"}))],
        invoke=lambda model, feedback: (
            "useful coaching" if model.model == "new-best" else (_ for _ in ()).throw(TimeoutError())
        ),
        validate=lambda value: (value == "useful coaching", "quality floor"),
        discover=lambda _contract: discovered,
        install=install,
    )

    assert result.model == "new-best"
    assert installed == ["new-best"]
    assert [n["event"] for n in notices][-2:] == ["model_installed", "model_failover_succeeded"]


def test_capability_mismatch_is_skipped_without_invocation():
    called: list[str] = []
    contract = JobContract(
        job_id="vision.caption",
        role="vision",
        required_capabilities={"vision"},
    )
    engine = SelfHealingEngine()
    result = engine.run(
        contract,
        [
            Candidate("text-only", "ollama", frozenset({"text"})),
            Candidate("vision-model", "mlx", frozenset({"vision"})),
        ],
        invoke=lambda model, feedback: called.append(model.model) or "caption",
        validate=lambda value: (True, ""),
    )
    assert result.model == "vision-model"
    assert called == ["vision-model"]


def test_lru_prune_is_oldest_first_and_never_touches_protected():
    managed = {
        "incumbent": {"size_gb": 40, "last_used": "2026-01-01T00:00:00+00:00"},
        "old": {"size_gb": 20, "last_used": "2026-02-01T00:00:00+00:00"},
        "recent": {"size_gb": 30, "last_used": "2026-06-01T00:00:00+00:00"},
    }
    plan = lru_prune_plan(managed, protected={"incumbent"}, need_gb=45)
    assert [item["model"] for item in plan] == ["old", "recent"]
    assert sum(item["size_gb"] for item in plan) >= 45


def test_lru_prune_does_not_require_upstream_availability():
    managed = {
        "discontinued": {"size_gb": 10, "last_used": "2025-01-01", "redownloadable": False},
    }
    assert [item["model"] for item in lru_prune_plan(
        managed, protected=set(), need_gb=1
    )] == ["discontinued"]


def test_candidate_order_prefers_discovery_score_not_latency():
    contract = JobContract(job_id="quality.first", role="reasoning")
    candidates = [
        Candidate("fast", "mlx", discovery_score=0.7, latency_ms=100),
        Candidate("best", "mlx", discovery_score=0.95, latency_ms=9000),
    ]
    engine = SelfHealingEngine()
    result = engine.run(
        contract,
        candidates,
        invoke=lambda model, feedback: model.model,
        validate=lambda value: (True, ""),
    )
    assert result.model == "best"
