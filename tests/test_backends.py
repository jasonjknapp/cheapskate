# SPDX-License-Identifier: Apache-2.0
"""Pins backend resolution + the single-large-model lifecycle + cross-runtime
eviction. Uses the injection points (launcher, port_checker, foreign_check,
binary_exists, evict, runner) — no real processes, sockets, or sleeps."""

from __future__ import annotations

import pytest

from cheapskate.backends import mlx as mlxmod
from cheapskate.backends import ollama as ollamamod
from cheapskate.backends import preflight as pf
from cheapskate.backends.resolve import (
    BackendSpec,
    LocalUnavailable,
    infer_backend,
    port_of,
    resolve,
)


# ── resolve ─────────────────────────────────────────────────────────────────


def test_infer_backend_slash_is_mlx():
    assert infer_backend("org/repo") == "mlx"


def test_infer_backend_tag_is_ollama():
    assert infer_backend("llama3:70b") == "ollama"


def test_infer_backend_hf_gguf_is_ollama():
    # An hf.co GGUF pull ref has a slash but is Ollama's, not MLX's.
    assert infer_backend("hf.co/org/repo:Q4") == "ollama"


def test_resolve_role_from_registry():
    cfg = {"roles": {"code": {"model": "org/coder", "backend": "mlx", "approx_gb": 18}}}
    spec = resolve(role="code", config=cfg)
    assert spec.model == "org/coder"
    assert spec.backend == "mlx"
    assert spec.approx_gb == 18
    assert spec.role == "code"


def test_resolve_role_without_model_raises():
    with pytest.raises(LocalUnavailable):
        resolve(role="ghost", config={"roles": {}})


def test_resolve_model_inherits_role_metadata():
    cfg = {"roles": {"reasoning": {"model": "m:tag", "backend": "ollama", "approx_gb": 40}}}
    spec = resolve(model="m:tag", config=cfg)
    assert spec.role == "reasoning"
    assert spec.approx_gb == 40


def test_resolve_unknown_model_infers_backend():
    spec = resolve(model="mystery:tag", config={"roles": {}})
    assert spec.backend == "ollama"
    assert spec.approx_gb is None


def test_resolve_config_backend_endpoint_override():
    # A non-localhost URL in config.backends IS the multi-machine story.
    cfg = {"roles": {}, "backends": {"ollama": "http://10.0.0.9:11434"}}
    spec = resolve(model="m:tag", config=cfg)
    assert spec.endpoint == "http://10.0.0.9:11434"


def test_port_of_extracts_port():
    assert port_of(BackendSpec(model="m", backend="mlx", endpoint="http://127.0.0.1:8080")) == 8080


# ── MLX lifecycle (all collaborators injected) ──────────────────────────────


def _ok_health(monkeypatch, healthy=True):
    monkeypatch.setattr(mlxmod, "mlx_health", lambda *a, **k: healthy)


def test_ensure_mlx_refuses_over_budget():
    with pytest.raises(LocalUnavailable) as e:
        mlxmod.ensure_mlx(
            "org/huge", approx_gb=200, budget_gb=100,
            binary_exists=lambda: True,
        )
    assert "budget" in str(e.value).lower()


def test_ensure_mlx_refuses_missing_binary():
    with pytest.raises(LocalUnavailable) as e:
        mlxmod.ensure_mlx("org/m", approx_gb=10, budget_gb=100, binary_exists=lambda: False)
    assert "binary" in str(e.value).lower()


def test_ensure_mlx_loads_and_records_state(monkeypatch):
    _ok_health(monkeypatch, healthy=True)
    launched = {}

    def fake_launch(model, port):
        launched["model"] = model
        launched["port"] = port
        return 4242

    monkeypatch.setattr(mlxmod, "_pid_alive", lambda pid: True)
    base = mlxmod.ensure_mlx(
        "org/model", approx_gb=18, port=8080, budget_gb=100,
        launcher=fake_launch,
        port_checker=lambda p: False,  # port free
        foreign_check=lambda: None,  # no foreign server
        binary_exists=lambda: True,
        evict=lambda needed: None,  # nothing to evict
    )
    assert base == "http://127.0.0.1:8080"
    assert launched == {"model": "org/model", "port": 8080}
    assert mlxmod._read_state()["model"] == "org/model"


def test_ensure_mlx_reuses_same_model(monkeypatch):
    _ok_health(monkeypatch, healthy=True)
    monkeypatch.setattr(mlxmod, "_pid_alive", lambda pid: True)
    mlxmod._write_state({"model": "org/model", "pid": 99, "port": 8080})

    calls = {"launched": False}

    def fake_launch(model, port):  # should NOT be called
        calls["launched"] = True
        return 1

    base = mlxmod.ensure_mlx(
        "org/model", approx_gb=18, port=8080, budget_gb=100,
        launcher=fake_launch, port_checker=lambda p: False,
        foreign_check=lambda: None, binary_exists=lambda: True, evict=lambda n: None,
    )
    assert base == "http://127.0.0.1:8080"
    assert calls["launched"] is False  # reused the resident server


def test_ensure_mlx_foreign_server_aborts(monkeypatch):
    def foreign():
        raise RuntimeError("unmanaged MLX server resident")

    with pytest.raises(RuntimeError):
        mlxmod.ensure_mlx(
            "org/model", approx_gb=18, budget_gb=100,
            foreign_check=foreign, binary_exists=lambda: True,
            port_checker=lambda p: False, launcher=lambda m, p: 1, evict=lambda n: None,
        )


def test_ensure_mlx_evict_is_called_before_load(monkeypatch):
    _ok_health(monkeypatch, healthy=True)
    monkeypatch.setattr(mlxmod, "_pid_alive", lambda pid: True)
    order = []

    mlxmod.ensure_mlx(
        "org/model", approx_gb=40, budget_gb=100,
        foreign_check=lambda: order.append("foreign"),
        evict=lambda needed: order.append(("evict", needed)),
        launcher=lambda m, p: (order.append("launch"), 7)[1],
        port_checker=lambda p: False, binary_exists=lambda: True,
    )
    # Foreign guard, then eviction, then launch — the load-bearing order.
    assert order == ["foreign", ("evict", 40.0), "launch"]


def test_assert_no_foreign_mlx_exempts_managed(monkeypatch):
    mlxmod._write_state({"pid": 555, "model": "m"})
    # pgrep returns our own managed pid → no foreign server.
    mlxmod.assert_no_foreign_mlx(pgrep=lambda: [555])


def test_assert_no_foreign_mlx_flags_foreign():
    with pytest.raises(RuntimeError):
        mlxmod.assert_no_foreign_mlx(pgrep=lambda: [99999])


def test_ensure_mlx_port_autocorrects_to_default(monkeypatch):
    _ok_health(monkeypatch, healthy=True)
    monkeypatch.setattr(mlxmod, "_pid_alive", lambda pid: True)
    launched = {}

    # Requested non-default port is foreign-occupied; the default is free.
    def port_checker(port):
        return port != mlxmod.MLX_PORT

    base = mlxmod.ensure_mlx(
        "org/model", approx_gb=18, port=9099, budget_gb=100,
        port_checker=port_checker,
        launcher=lambda m, p: launched.setdefault("port", p) or 5,
        foreign_check=lambda: None, binary_exists=lambda: True, evict=lambda n: None,
    )
    assert base == f"http://127.0.0.1:{mlxmod.MLX_PORT}"
    assert launched["port"] == mlxmod.MLX_PORT


# ── cross-runtime eviction (evict_coresidents) ──────────────────────────────


def test_evict_noop_when_everything_fits():
    calls = []
    pf.evict_coresidents(
        30, 100,
        ollama_resident=lambda: 10,
        lms_loaded=lambda: True,
        lms_resident=lambda: 20,  # 30 + 10 + 20 = 60 <= 100
        lms_unload=lambda: calls.append("lms"),
        ollama_stop=lambda: calls.append("ollama"),
        sleep=lambda s: None,
    )
    assert calls == []  # nothing evicted — everything coexists


def test_evict_secondary_runtime_first_under_pressure():
    calls = []
    state = {"lms_gone": False}

    def lms_res():
        return 0 if state["lms_gone"] else 40

    def lms_unload():
        state["lms_gone"] = True
        calls.append("lms")

    pf.evict_coresidents(
        50, 100,
        ollama_resident=lambda: 20,  # 50 + 20 + 40 = 110 > 100 → pressure
        lms_loaded=lambda: True,
        lms_resident=lms_res,
        lms_unload=lms_unload,
        ollama_stop=lambda: calls.append("ollama"),
        sleep=lambda s: None,
    )
    # secondary runtime freed → now 50 + 20 = 70 <= 100 → ollama NOT stopped.
    assert calls == ["lms"]


def test_evict_stops_ollama_then_raises_if_still_over():
    calls = []
    with pytest.raises(RuntimeError):
        pf.evict_coresidents(
            90, 100,
            ollama_resident=lambda: 60,  # 90 + 60 = 150 > 100, stays over
            lms_loaded=lambda: False,
            lms_resident=lambda: 0,
            lms_unload=lambda: calls.append("lms"),
            ollama_stop=lambda: calls.append("ollama"),
            sleep=lambda s: None,
        )
    assert "ollama" in calls  # it tried stopping ollama before failing closed


# ── ollama probes ───────────────────────────────────────────────────────────


def test_ollama_resident_gb_parses_sizes():
    fake_ps = "NAME SIZE\nmodel-a 12 GB\nmodel-b 512 MB\n"
    total = ollamamod.ollama_resident_gb(runner=lambda: fake_ps)
    assert round(total, 2) == round(12 + 512 / 1024, 2)


def test_ollama_model_resident_matches_base_name():
    fake_ps = "NAME SIZE\nllama3:70b 40 GB\n"
    assert ollamamod.ollama_model_resident("llama3", runner=lambda: fake_ps) is True
    assert ollamamod.ollama_model_resident("mistral", runner=lambda: fake_ps) is False


def test_lms_loaded_detects_no_models():
    assert ollamamod.lms_loaded(runner=lambda: "No models loaded") is False
    assert ollamamod.lms_loaded(runner=lambda: "model-x 8 GB") is True
