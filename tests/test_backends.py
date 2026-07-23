# SPDX-License-Identifier: Apache-2.0
"""Pins backend resolution + the single-large-model lifecycle + cross-runtime
eviction. Uses the injection points (launcher, port_checker, foreign_check,
binary_exists, evict, runner), no real processes, sockets, or sleeps."""

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
    role_candidates,
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


def test_role_candidates_are_incumbent_fallback_rollback_and_skip_quarantine():
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent",
        "backend": "mlx",
        "fallback": "fallback:latest",
        "rollback": ["org/rollback", "org/bad"],
        "quarantine": ["org/bad"],
    }}}
    candidates = role_candidates("reasoning", config=cfg)
    assert [spec.model for spec in candidates] == [
        "org/incumbent", "fallback:latest", "org/rollback",
    ]
    assert [spec.backend for spec in candidates] == ["mlx", "ollama", "mlx"]


def test_role_fallback_uses_broker_exact_model_route_metadata():
    cfg = {"roles": {
        "classification": {
            "model": "local:latest", "backend": "ollama",
            "fallback": "shared-model",
        },
        "remote-role": {
            "model": "shared-model", "backend": "remote",
            "endpoint": "https://models.example.com/v1",
        },
    }}
    fallback = role_candidates("classification", config=cfg)[1]
    assert fallback.backend == "remote"
    assert fallback.endpoint == "https://models.example.com/v1"


def test_resolve_uses_rollback_snapshot_backend_not_string_inference():
    """H2: a retained rollback snapshot resolves to its stored backend/endpoint/
    size, NOT string inference — a former lmstudio ``vendor/model`` would else be
    mis-inferred as MLX and lose its endpoint."""
    cfg = {"roles": {"reasoning": {
        "model": "org/current", "backend": "mlx",
        "rollback": ["vendor/former"],
        "rollback_configs": {"vendor/former": {
            "backend": "lmstudio",
            "endpoint": "http://127.0.0.1:1234/v1",
            "approx_gb": 12.0,
        }},
    }}}
    spec = resolve(model="vendor/former", config=cfg)
    assert spec.backend == "lmstudio"  # NOT mlx (the slash-infer default)
    assert spec.endpoint == "http://127.0.0.1:1234/v1"
    assert spec.approx_gb == 12.0
    assert spec.role == "reasoning"


def test_role_candidates_carry_rollback_snapshot_spec():
    """H2: the single-point snapshot resolution is inherited by role_candidates()."""
    cfg = {"roles": {"reasoning": {
        "model": "org/current", "backend": "mlx",
        "rollback": ["vendor/former"],
        "rollback_configs": {"vendor/former": {
            "backend": "lmstudio",
            "endpoint": "http://127.0.0.1:1234/v1",
            "approx_gb": 12.0,
        }},
    }}}
    candidates = role_candidates("reasoning", config=cfg)
    former = next(c for c in candidates if c.model == "vendor/former")
    assert former.backend == "lmstudio"
    assert former.endpoint == "http://127.0.0.1:1234/v1"


def test_live_incumbent_wins_over_rollback_snapshot():
    """H2: a live incumbent match always wins over a stale snapshot for the same
    model — stale metadata never shadows live state."""
    cfg = {"roles": {
        "reasoning": {"model": "shared/model", "backend": "mlx"},
        "code": {
            "model": "org/coder", "backend": "ollama",
            "rollback": ["shared/model"],
            "rollback_configs": {"shared/model": {
                "backend": "lmstudio", "endpoint": "http://127.0.0.1:1234/v1",
            }},
        },
    }}
    spec = resolve(model="shared/model", config=cfg)
    assert spec.backend == "mlx"  # live reasoning incumbent, not the code snapshot
    assert spec.role == "reasoning"


def test_resolve_no_snapshot_falls_back_to_string_inference():
    """H2 back-compat: a model in no ``rollback_configs`` still infers by string
    (older registries / hand-edited entries)."""
    cfg = {"roles": {"reasoning": {"model": "org/current", "backend": "mlx"}}}
    spec = resolve(model="vendor/former", config=cfg)
    assert spec.backend == "mlx"  # slash → MLX inference, no snapshot present
    assert spec.role is None


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
    # Foreign guard, then eviction, then launch, the load-bearing order.
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
    assert calls == []  # nothing evicted, everything coexists


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


def test_ollama_model_resident_matches_exact_normalized_tag():
    fake_ps = "NAME SIZE\nllama3:70b 40 GB\ndefaulted:latest 8 GB\n"
    assert ollamamod.ollama_model_resident("llama3:70b", runner=lambda: fake_ps) is True
    assert ollamamod.ollama_model_resident("llama3:8b", runner=lambda: fake_ps) is False
    assert ollamamod.ollama_model_resident("defaulted", runner=lambda: fake_ps) is True
    assert ollamamod.ollama_model_resident("llama3", runner=lambda: fake_ps) is False
    assert ollamamod.ollama_model_resident("mistral", runner=lambda: fake_ps) is False


def test_oversized_sibling_tag_cannot_bypass_broker_capacity(monkeypatch):
    from cheapskate.broker import app as broker_app

    fake_ps = "NAME SIZE\nqwen3:8b 5 GB\n"
    monkeypatch.setattr(broker_app, "lms_loaded", lambda: False)
    monkeypatch.setattr(broker_app, "ollama_resident_gb", lambda: 5.0)
    monkeypatch.setattr(
        broker_app,
        "ollama_model_resident",
        lambda model: ollamamod.ollama_model_resident(model, runner=lambda: fake_ps),
    )
    spec = BackendSpec(
        model="qwen3:30b", backend="ollama",
        endpoint="http://127.0.0.1:11434", approx_gb=120.0,
    )

    with pytest.raises(RuntimeError, match="503"):
        broker_app.enforce_capacity(spec, budget_gb=100.0)


def test_lms_loaded_detects_no_models():
    assert ollamamod.lms_loaded(runner=lambda: "No models loaded") is False
    assert ollamamod.lms_loaded(runner=lambda: "model-x 8 GB") is True


def test_ollama_model_present_reads_ollama_list():
    fake_list = (
        "NAME\t\tID\t\tSIZE\n"
        "qwen3-coder:30b\tabc\t18 GB\n"
        "defaulted:latest\tdef\t8 GB\n"
    )
    assert ollamamod.ollama_model_present(
        "qwen3-coder:30b", runner=lambda: fake_list
    ) is True
    assert ollamamod.ollama_model_present(
        "qwen3-coder", runner=lambda: fake_list
    ) is False
    assert ollamamod.ollama_model_present(
        "defaulted", runner=lambda: fake_list
    ) is True
    assert ollamamod.ollama_model_present("mistral", runner=lambda: fake_list) is False


# ── ollama_pull (injected runner) ───────────────────────────────────────────


def test_ollama_pull_success_path():
    class _Proc:
        returncode = 0

    calls = []

    def runner(argv):
        calls.append(argv)
        return _Proc()

    assert ollamamod.ollama_pull("qwen3-coder:30b", runner=runner) is True
    assert calls == [["ollama", "pull", "qwen3-coder:30b"]]


def test_ollama_pull_failure_returns_false_never_raises():
    class _Proc:
        returncode = 1

    assert ollamamod.ollama_pull("m:tag", runner=lambda argv: _Proc()) is False


def test_ollama_pull_exception_is_swallowed_to_false():
    def boom(argv):
        raise RuntimeError("network down")

    # never raises, the caller fails closed on False
    assert ollamamod.ollama_pull("m:tag", runner=boom) is False


# ── default suggested roles ──────────────────────────────────────────────────


def test_default_roles_shape():
    from cheapskate.registry import registry as reg

    dr = reg.default_roles()
    assert set(dr) == {"reasoning", "code", "classification", "creative"}
    for role, rc in dr.items():
        assert rc["model"] and isinstance(rc["model"], str)
        assert rc["backend"] in ("ollama", "mlx")
        assert isinstance(rc["approx_gb"], float) and rc["approx_gb"] > 0
    # the specific reference fleet is shipped
    assert dr["code"]["model"] == "qwen3-coder:30b"
    assert dr["classification"]["backend"] == "ollama"


def test_classification_default_resolves_to_ollama_runtime():
    spec = resolve(role="classification", config={"roles": {}})
    assert spec.model == "qwen3.5:9b-mlx"
    assert spec.backend == "ollama"
    assert spec.endpoint.endswith(":11434")


def test_resolve_falls_back_to_default_when_config_and_registry_empty():
    # No config.roles, empty registry (conftest points XDG at a temp dir) → the
    # shipped default resolves.
    spec = resolve(role="code", config={"roles": {}})
    assert spec.model == "qwen3-coder:30b"
    assert spec.backend == "ollama"
    assert spec.approx_gb == 18.0


def test_config_role_overrides_default():
    cfg = {"roles": {"code": {"model": "me/mycoder", "backend": "mlx", "approx_gb": 12}}}
    spec = resolve(role="code", config=cfg)
    assert spec.model == "me/mycoder"  # user config wins over the default
    assert spec.backend == "mlx"


def test_registry_role_overrides_default(tmp_path, monkeypatch):
    # A promoted registry entry wins over the shipped default for that role.
    from cheapskate.registry import registry as reg

    p = tmp_path / "registry.yaml"
    r = reg.load(path=p)
    reg.set_incumbent(r, "code", "org/promoted-coder", "ollama", approx_gb=20.0)
    reg.save(r, path=p)
    monkeypatch.setattr(reg, "_registry_path", lambda path=None: p)

    spec = resolve(role="code", config={"roles": {}})
    assert spec.model == "org/promoted-coder"


def test_role_sources_marks_provenance(tmp_path, monkeypatch):
    from cheapskate.backends.resolve import role_sources
    from cheapskate.registry import registry as reg

    p = tmp_path / "registry.yaml"
    r = reg.load(path=p)
    reg.set_incumbent(r, "reasoning", "org/promoted", "ollama", approx_gb=30.0)
    reg.save(r, path=p)
    monkeypatch.setattr(reg, "_registry_path", lambda path=None: p)

    cfg = {"roles": {"code": {"model": "me/mine", "backend": "ollama"}}}
    src = role_sources(cfg)
    assert src["code"] == "config"  # config wins
    assert src["reasoning"] == "registry"  # promoted incumbent
    assert src["classification"] == "default"  # still just a suggestion
    assert src["creative"] == "default"


# ── ensure_role auto-pull (ensure-present-or-pull), gate reused ──────────────

_OLLAMA_ROLE_CFG = {
    "roles": {"code": {"model": "org/coder:30b", "backend": "ollama", "approx_gb": 18.0}},
    "machine": {"ram_headroom_gb": 24.0, "disk_headroom_gb": 15.0, "auto_pull": True},
}


def test_ensure_role_pulls_when_absent_and_gate_ok(monkeypatch):
    # Not resident, not present → gate OK (18GB fits) → pull once → then present.
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    presence = {"present": False}
    pulls = []

    def model_present(_m):
        return presence["present"]

    def pull(_m):
        pulls.append(_m)
        presence["present"] = True
        return True

    spec = pf.ensure_role(
        role="code", config=_OLLAMA_ROLE_CFG, budget_gb=100.0,
        pull=pull, model_present=model_present,
        free_disk=lambda: 500.0, log=lambda *_: None,
    )
    assert spec.model == "org/coder:30b"
    assert pulls == ["org/coder:30b"]  # pulled exactly once


def test_ensure_role_gate_refuses_too_big_no_pull(monkeypatch):
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    pulls = []
    # approx 18GB but a tiny RAM budget (10GB) → gate refuses on RAM.
    with pytest.raises(LocalUnavailable) as e:
        pf.ensure_role(
            role="code", config=_OLLAMA_ROLE_CFG, budget_gb=10.0,
            pull=lambda m: pulls.append(m) or True,
            model_present=lambda _m: False,
            free_disk=lambda: 500.0, log=lambda *_: None,
        )
    assert pulls == []  # never pulled
    assert "gate" in str(e.value).lower()


def test_ensure_role_gate_refuses_disk_headroom_no_pull(monkeypatch):
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    pulls = []
    # 18GB candidate but only 20GB free with 15GB headroom → 20-18 < 15 → refuse.
    with pytest.raises(LocalUnavailable) as e:
        pf.ensure_role(
            role="code", config=_OLLAMA_ROLE_CFG, budget_gb=100.0,
            pull=lambda m: pulls.append(m) or True,
            model_present=lambda _m: False,
            free_disk=lambda: 20.0, log=lambda *_: None,
        )
    assert pulls == []
    assert "disk" in str(e.value).lower()


def test_ensure_role_auto_pull_disabled_no_pull(monkeypatch):
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    cfg = {
        "roles": {"code": {"model": "org/coder:30b", "backend": "ollama", "approx_gb": 18.0}},
        "machine": {"auto_pull": False},
    }
    pulls = []
    with pytest.raises(LocalUnavailable) as e:
        pf.ensure_role(
            role="code", config=cfg, budget_gb=100.0,
            pull=lambda m: pulls.append(m) or True,
            model_present=lambda _m: False,
            free_disk=lambda: 500.0,
        )
    assert pulls == []
    assert "auto_pull" in str(e.value)


def test_ensure_role_no_pull_when_already_present(monkeypatch):
    # Pulled-on-disk (present) but not loaded → Ollama auto-loads; no pull needed.
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    pulls = []
    spec = pf.ensure_role(
        role="code", config=_OLLAMA_ROLE_CFG, budget_gb=100.0,
        pull=lambda m: pulls.append(m) or True,
        model_present=lambda _m: True,  # already pulled
        free_disk=lambda: 500.0,
    )
    assert spec.model == "org/coder:30b"
    assert pulls == []  # nothing pulled


def test_ensure_role_no_pull_when_present(monkeypatch):
    # A model that is PULLED (present on disk) needs no fetch: the Ollama daemon
    # auto-loads it on request. The hot path checks presence with a single probe
    # and does not also probe residency (`ollama ps`).
    ps_calls = []
    monkeypatch.setattr(
        ollamamod, "ollama_model_resident",
        lambda *_a, **_k: ps_calls.append(1) or True,
    )
    pulls = []
    spec = pf.ensure_role(
        role="code", config=_OLLAMA_ROLE_CFG, budget_gb=100.0,
        pull=lambda m: pulls.append(m) or True,
        model_present=lambda _m: True,
        free_disk=lambda: 500.0,
    )
    assert spec.model == "org/coder:30b"
    assert pulls == []
    assert ps_calls == []  # residency is NOT probed on the hot path


def test_ensure_role_pull_failure_raises(monkeypatch):
    monkeypatch.setattr(ollamamod, "ollama_model_resident", lambda *_a, **_k: False)
    with pytest.raises(LocalUnavailable) as e:
        pf.ensure_role(
            role="code", config=_OLLAMA_ROLE_CFG, budget_gb=100.0,
            pull=lambda _m: False,  # gate OK but the download fails
            model_present=lambda _m: False,
            free_disk=lambda: 500.0, log=lambda *_: None,
        )
    assert "failed" in str(e.value).lower()
