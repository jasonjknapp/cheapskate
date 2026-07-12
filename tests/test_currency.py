# SPDX-License-Identifier: Apache-2.0
"""Currency: discover with a fake hub api, candidate-size fail-closed gate,
evaluate → promote/rollback, prune allowlist that never touches protected models."""

from __future__ import annotations

from types import SimpleNamespace

from cheapskate.registry import currency as cur
from cheapskate.registry import registry as reg


# ── fake hub api ─────────────────────────────────────────────────────────────


class FakeApi:
    def __init__(self, models_by_author=None, sizes=None):
        self._models = models_by_author or {}
        self._sizes = sizes or {}

    def list_models(self, *, author, sort, limit):
        return self._models.get(author, [])

    def model_info(self, repo, *, files_metadata):
        siblings = [SimpleNamespace(size=int(sz)) for sz in self._sizes.get(repo, [])]
        return SimpleNamespace(siblings=siblings)


def _model(repo, last="2026-01-01"):
    return SimpleNamespace(id=repo, lastModified=last)


# ── discover ─────────────────────────────────────────────────────────────────


def test_discover_shortlists_from_allowlist():
    r = {"roles": {"reasoning": {"model": "vendor/qwen3-70b", "backend": "mlx"}}}
    api = FakeApi(models_by_author={
        "vendor": [_model("vendor/qwen3-80b"), _model("vendor/llama3-8b")],
    })
    out = cur.discover("reasoning", r, ["vendor"], api=api)
    repos = {c["repo"] for c in out}
    assert repos == {"vendor/qwen3-80b", "vendor/llama3-8b"}
    lineage = {c["repo"]: c["same_lineage"] for c in out}
    assert lineage["vendor/qwen3-80b"] is True  # same qwen3 family
    assert lineage["vendor/llama3-8b"] is False


def test_discover_degrades_to_empty_on_api_failure():
    class Boom(FakeApi):
        def list_models(self, **k):
            raise RuntimeError("network")

    r = {"roles": {"reasoning": {"model": "v/m", "backend": "mlx"}}}
    assert cur.discover("reasoning", r, ["vendor"], api=Boom()) == []


# ── candidate sizing + fit gate (fail-closed, sizes the CANDIDATE) ───────────


def test_candidate_size_from_hub_siblings():
    api = FakeApi(sizes={"vendor/big": [2_000_000_000, 3_000_000_000]})
    assert cur.candidate_size_gb("vendor/big", "mlx", api=api) == 5.0


def test_candidate_size_ollama_uses_known_sizes():
    assert cur.candidate_size_gb("tag:9b", "ollama", known_sizes={"tag:9b": 6.6}) == 6.6
    assert cur.candidate_size_gb("tag:unknown", "ollama", known_sizes={}) is None


def test_fits_fails_closed_on_unknown_size():
    ok, reason, size = cur.candidate_fits(
        "tag:unknown", "ollama", free_disk_gb=500, ram_budget_gb=128, known_sizes={}
    )
    assert ok is False
    assert size is None
    assert "undeterminable" in reason


def test_fits_rejects_over_max_download():
    ok, reason, _ = cur.candidate_fits(
        "v/huge", "mlx", free_disk_gb=1000, ram_budget_gb=128, max_download_gb=80,
        assume_size_gb=200,
    )
    assert ok is False
    assert "max_download_gb" in reason


def test_fits_rejects_on_disk_headroom():
    ok, reason, _ = cur.candidate_fits(
        "v/m", "mlx", free_disk_gb=20, ram_budget_gb=128, disk_headroom_gb=15,
        assume_size_gb=10,
    )
    assert ok is False
    assert "disk headroom" in reason


def test_fits_rejects_on_ram_budget():
    ok, reason, _ = cur.candidate_fits(
        "v/m", "mlx", free_disk_gb=1000, ram_budget_gb=128, ram_headroom_gb=24,
        max_download_gb=500, assume_size_gb=110,
    )
    assert ok is False
    assert "RAM budget" in reason


def test_fits_accepts_within_budget():
    ok, reason, size = cur.candidate_fits(
        "v/m", "mlx", free_disk_gb=1000, ram_budget_gb=128, assume_size_gb=40,
    )
    assert ok is True
    assert size == 40


# ── evaluate ─────────────────────────────────────────────────────────────────


def _pass(rate, crit=1, crit_total=1):
    return {"pass_rate": rate, "critical_passed": crit, "critical_total": crit_total}


def _decide(inc, cand, lineage):
    # simple: promote iff candidate strictly better AND no critical regression
    if cand["critical_passed"] < cand["critical_total"]:
        return {"promote": False, "reason": "critical floor breached"}
    return {"promote": cand["pass_rate"] > inc["pass_rate"], "reason": "eval"}


def _reg():
    return {"roles": {"reasoning": {"model": "v/inc", "backend": "mlx"}}}


def test_evaluate_promotes_on_win():
    r = _reg()
    plan = cur.evaluate(
        "reasoning", "v/cand", r,
        eval_fn=lambda m, b: _pass(0.9) if m == "v/cand" else _pass(0.7),
        decision_fn=_decide,
    )
    assert plan["decision"]["promote"] is True


def test_evaluate_fails_closed_when_not_fitting():
    r = _reg()
    calls = []
    plan = cur.evaluate(
        "reasoning", "v/cand", r,
        eval_fn=lambda m, b: calls.append(m) or _pass(0.9),
        decision_fn=_decide,
        fits=False, fit_reason="disk headroom",
    )
    assert plan["decision"]["promote"] is False
    assert "pre-download gate" in plan["decision"]["reason"]
    assert not calls  # never ran the suite


def test_evaluate_refuses_quarantined_candidate():
    r = _reg()
    reg.quarantine(r, "reasoning", "v/bad")
    plan = cur.evaluate(
        "reasoning", "v/bad", r,
        eval_fn=lambda m, b: _pass(0.99), decision_fn=_decide,
    )
    assert plan["decision"]["promote"] is False
    assert "quarantined" in plan["decision"]["reason"]


def test_evaluate_eval_error_is_no_promote():
    r = _reg()

    def boom(m, b):
        raise RuntimeError("model crashed")

    plan = cur.evaluate("reasoning", "v/cand", r, eval_fn=boom, decision_fn=_decide)
    assert plan["decision"]["promote"] is False
    assert "evaluation error" in plan["decision"]["reason"]


# ── promote / rollback ───────────────────────────────────────────────────────


def test_promote_dry_run_does_not_mutate():
    r = _reg()
    plan = cur.promote("reasoning", "v/cand", "mlx", r, dry_run=True)
    assert plan["applied"] is False
    assert r["roles"]["reasoning"]["model"] == "v/inc"


def test_promote_applies_and_retains_rollback():
    r = _reg()
    cur.promote("reasoning", "v/cand", "mlx", r, dry_run=False)
    assert r["roles"]["reasoning"]["model"] == "v/cand"
    assert r["roles"]["reasoning"]["rollback"] == ["v/inc"]
    rb = cur.rollback("reasoning", r, dry_run=False)
    assert rb["to"] == "v/inc"
    assert r["roles"]["reasoning"]["model"] == "v/inc"


# ── prune allowlist ──────────────────────────────────────────────────────────


def test_prune_never_touches_protected_or_unmanaged():
    r = {"roles": {}}
    reg.set_incumbent(r, "reasoning", "inc", "mlx", fallback="fb")
    reg.set_incumbent(r, "reasoning", "inc2", "mlx", fallback="fb")  # inc → rollback
    managed = {
        "inc2": {"backend": "mlx", "status": "incumbent"},   # protected (incumbent)
        "inc": {"backend": "mlx", "status": "rollback"},      # protected (rollback)
        "fb": {"backend": "mlx", "status": "fallback"},       # protected (fallback)
        "old-superseded": {"backend": "mlx", "status": "superseded"},  # prunable
        "pinned": {"backend": "mlx", "status": "superseded", "prune": "never"},  # pinned
    }
    cands = {c["model"] for c in cur.prune_candidates(r, managed)}
    assert cands == {"old-superseded"}
    # a model NOT in managed (hand-pulled / shared cache) is never a candidate
    assert "some/unmanaged" not in cands
