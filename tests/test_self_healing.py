# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cheapskate.contracts import FailureKind, JobContract, classify_failure
from cheapskate.self_healing import (
    Candidate,
    CompatibilityStore,
    NoCompatibleModel,
    SelfHealingEngine,
    lru_prune_plan,
)


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
            Candidate("fast-but-wrong", "mlx", frozenset({"json"}), local=True),
            Candidate("reliable", "ollama", frozenset({"json"}), local=True),
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
        Candidate("new-best", "mlx", frozenset({"reasoning"}), discovery_score=0.93,
                  local=True),
        Candidate("older", "mlx", frozenset({"reasoning"}), discovery_score=0.88,
                  local=True),
    ]

    def install(candidate):
        installed.append(candidate.model)
        return True

    engine = SelfHealingEngine(notify=notices.append)
    result = engine.run(
        contract,
        [Candidate("broken", "mlx", frozenset({"reasoning"}), local=True)],
        invoke=lambda model, feedback: (
            "useful coaching" if model.model == "new-best" else (_ for _ in ()).throw(TimeoutError())
        ),
        validate=lambda value: (value == "useful coaching", "quality floor"),
        discover=lambda _contract: discovered,
        install=install,
        fit=lambda candidate, _contract: (True, "fits"),
        evaluate=lambda candidate, _contract: (True, "job canary passed", 1.0),
        promote=lambda candidate, _contract: True,
    )

    assert result.model == "new-best"
    assert installed == ["new-best"]
    assert [n["event"] for n in notices][-3:] == [
        "model_installed",
        "model_promoted",
        "model_failover_succeeded",
    ]


def test_discovery_fails_closed_without_fit_eval_and_promotion_gates():
    installed: list[str] = []
    engine = SelfHealingEngine()
    with pytest.raises(NoCompatibleModel, match="requires fit, eval/canary, and promotion"):
        engine.run(
            JobContract(job_id="guarded.discovery", role="reasoning", repair_attempts=0),
            [],
            invoke=lambda model, feedback: "unused",
            validate=lambda value: (True, ""),
            discover=lambda contract: [Candidate("unguarded", "mlx", local=True)],
            install=lambda candidate: installed.append(candidate.model) or True,
        )
    assert installed == []


@pytest.mark.parametrize("stage", ["discover", "fit", "install", "evaluate", "promote"])
def test_recovery_adapter_exceptions_are_classified_and_notify_failure(stage):
    notices: list[dict] = []
    candidate = Candidate(
        "candidate", "mlx", frozenset({"reasoning"}), installed=False, local=True,
    )

    def boom(*_args):
        raise RuntimeError(f"{stage} exploded")

    kwargs = {
        "discover": (boom if stage == "discover" else lambda _contract: [candidate]),
        "fit": (boom if stage == "fit" else lambda _candidate, _contract: (True, "fits")),
        "install": (boom if stage == "install" else lambda _candidate: True),
        "evaluate": (boom if stage == "evaluate"
                     else lambda _candidate, _contract: (True, "passed", 1.0)),
        "promote": (boom if stage == "promote" else lambda _candidate, _contract: True),
    }
    with pytest.raises(NoCompatibleModel) as exc_info:
        SelfHealingEngine(notify=notices.append).run(
            JobContract(job_id=f"adapter.{stage}", role="reasoning",
                        required_capabilities={"reasoning"}, repair_attempts=0),
            [], invoke=lambda _candidate, _feedback: "unused",
            validate=lambda _value: (True, ""), **kwargs,
        )

    assert f"{stage} adapter failed" in str(exc_info.value)
    assert notices[-1]["event"] == "model_recovery_failed"


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
            Candidate("text-only", "ollama", frozenset({"text"}), local=True),
            Candidate("vision-model", "mlx", frozenset({"vision"}), local=True),
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


def test_installed_candidate_order_preserves_incumbent_then_fallback_contract():
    contract = JobContract(job_id="role.order", role="reasoning")
    candidates = [
        Candidate("fast", "mlx", discovery_score=0.7, latency_ms=100, local=True),
        Candidate("best", "mlx", discovery_score=0.95, latency_ms=9000, local=True),
    ]
    engine = SelfHealingEngine()
    result = engine.run(
        contract,
        candidates,
        invoke=lambda model, feedback: model.model,
        validate=lambda value: (True, ""),
    )
    assert result.model == "fast"


def test_never_cloud_contract_filters_remote_candidates():
    contract = JobContract(job_id="private.job", role="reasoning")
    called: list[str] = []
    result = SelfHealingEngine().run(
        contract,
        [
            Candidate("remote", "cloud", local=False),
            Candidate("local", "mlx", local=True),
        ],
        invoke=lambda model, feedback: called.append(model.model) or "ok",
        validate=lambda value: (True, ""),
    )
    assert result.model == "local"
    assert called == ["local"]


def test_job_quarantine_expires(tmp_path):
    clock = [datetime(2026, 7, 21, tzinfo=timezone.utc)]
    store = CompatibilityStore(
        tmp_path / "compatibility.json",
        ttl_s=60,
        now=lambda: clock[0],
    )
    store.block("job", "model", FailureKind.QUALITY, "bad output")
    assert store.is_blocked("job", "model")
    clock[0] += timedelta(seconds=61)
    assert not store.is_blocked("job", "model")


def test_compatibility_store_merges_writes_from_two_live_instances(tmp_path):
    path = tmp_path / "compatibility.json"
    first = CompatibilityStore(path)
    second = CompatibilityStore(path)

    first.block("job.one", "model-a", FailureKind.QUALITY, "bad output")
    second.block("job.two", "model-b", FailureKind.SCHEMA, "wrong shape")

    observed = CompatibilityStore(path)
    assert observed.is_blocked("job.one", "model-a")
    assert observed.is_blocked("job.two", "model-b")


def test_notification_failure_does_not_abort_successful_failover():
    calls: list[str] = []

    def broken_notify(_event):
        raise RuntimeError("notification service unavailable")

    result = SelfHealingEngine(notify=broken_notify).run(
        JobContract(job_id="notify.failsoft", role="reasoning", repair_attempts=0),
        [Candidate("bad", "mlx", local=True),
         Candidate("good", "ollama", local=True)],
        invoke=lambda candidate, _feedback: calls.append(candidate.model) or candidate.model,
        validate=lambda value: (value == "good", "quality floor"),
    )

    assert result.model == "good"
    assert calls == ["bad", "good"]
    assert result.notification_failures == ({
        "event": "model_failover_succeeded",
        "job_id": "notify.failsoft",
        "error": "RuntimeError: notification service unavailable",
    },)


def test_deadline_rejects_result_that_finishes_beyond_late_window():
    clock = [0.0]
    invoked: list[str] = []

    def invoke(candidate, _feedback):
        invoked.append(candidate.model)
        clock[0] = 16.0
        return "otherwise valid"

    engine = SelfHealingEngine(monotonic=lambda: clock[0])
    with pytest.raises(NoCompatibleModel, match="contract deadline exhausted"):
        engine.run(
            JobContract(
                job_id="bounded.job",
                role="reasoning",
                repair_attempts=2,
                deadline_s=10,
                bounded_late_s=5,
            ),
            [Candidate("slow", "mlx", local=True),
             Candidate("fallback", "ollama", local=True)],
            invoke=invoke,
            validate=lambda value: (True, ""),
        )

    assert invoked == ["slow"]


def test_bounded_late_window_accepts_quality_result_within_grace():
    clock = [0.0]

    def invoke(_candidate, _feedback):
        clock[0] = 11.0
        return "valid"

    result = SelfHealingEngine(monotonic=lambda: clock[0]).run(
        JobContract(
            job_id="bounded.late",
            role="reasoning",
            deadline_s=10,
            bounded_late_s=2,
        ),
        [Candidate("quality", "mlx", local=True)],
        invoke=invoke,
        validate=lambda value: (True, "", 1.0),
    )
    assert result.model == "quality"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"deadline_s": -1}, "deadline_s"),
        ({"bounded_late_s": -1}, "bounded_late_s"),
    ],
)
def test_contract_rejects_negative_time_budgets(kwargs, message):
    with pytest.raises(ValueError, match=message):
        JobContract(job_id="invalid", role="reasoning", **kwargs)


def test_lru_prune_protects_fleet_state_flags_without_upstream_gate():
    managed = {
        "fallback": {"size_gb": 30, "last_used": "2024-01-01", "fallback": True},
        "loaded": {"size_gb": 30, "last_used": "2024-01-01", "loaded": True},
        "obsolete": {
            "size_gb": 30,
            "last_used": "2024-01-01",
            "redownloadable": False,
        },
    }
    plan = lru_prune_plan(managed, protected=set(), need_gb=10)
    assert [item["model"] for item in plan] == ["obsolete"]


def test_lru_metadata_cannot_override_validated_model_or_size():
    managed = {
        "canonical": {
            "model": "spoofed",
            "size_gb": 12,
            "last_used": "2024-01-01",
        },
        "invalid": {"size_gb": "unknown", "last_used": "2023-01-01"},
        "infinite": {"size_gb": float("inf"), "last_used": "2023-01-01"},
    }
    plan = lru_prune_plan(managed, protected=set(), need_gb=1)
    assert plan == [{
        "model": "canonical",
        "size_gb": 12.0,
        "last_used": "2024-01-01",
    }]


def test_lru_prune_normalizes_mixed_timestamps_and_missing_is_oldest():
    managed = {
        "numeric": {"size_gb": 1, "last_used": 1_800_000_000},
        "iso": {"size_gb": 1, "last_used": "2025-01-01T00:00:00+00:00"},
        "missing": {"size_gb": 1},
        "malformed": {"size_gb": 1, "last_used": "not-a-date"},
    }
    plan = lru_prune_plan(managed, protected=set(), need_gb=4)
    assert [item["model"] for item in plan] == [
        "malformed", "missing", "iso", "numeric",
    ]


def test_never_cloud_rejects_remote_endpoint_even_with_local_backend_alias():
    called: list[str] = []
    with pytest.raises(NoCompatibleModel):
        SelfHealingEngine().run(
            JobContract(job_id="private.remote-alias", role="reasoning"),
            [Candidate("remote-ollama", "ollama", local=False)],
            invoke=lambda model, feedback: called.append(model.model) or "ok",
            validate=lambda value: (True, ""),
        )
    assert called == []
