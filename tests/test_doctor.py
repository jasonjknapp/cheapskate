# SPDX-License-Identifier: Apache-2.0
"""``cheapskate doctor``: the contract is WARN-not-fail on a bare machine.

The autouse ``_isolate_state`` fixture in conftest points XDG at a temp dir, so
these tests exercise doctor exactly as a fresh clone would see it — no user
config, an empty registry, no serving engines guaranteed. Doctor must exit 0 and
never crash."""

from __future__ import annotations

from cheapskate import doctor


def test_doctor_exits_zero_on_a_bare_machine(monkeypatch):
    # Force EVERY serving engine to look absent + unreachable (a true bare box).
    monkeypatch.setattr(doctor.shutil, "which", lambda *_a, **_k: None)

    import httpx

    def _unreachable(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _unreachable)

    checks, exit_code = doctor.run_doctor()
    assert exit_code == 0  # missing engines are WARN, never FAIL
    names = {c.name for c in checks}
    assert "config" in names
    assert "engine:ollama" in names
    # the engine checks degraded to WARN, not FAIL
    engine_checks = [c for c in checks if c.name.startswith("engine:")]
    assert engine_checks and all(c.status == doctor.WARN for c in engine_checks)


def test_doctor_reports_the_effective_checks(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda *_a, **_k: None)
    checks, _ = doctor.run_doctor()
    by_name = {c.name: c for c in checks}
    # config passes and reports the effective paths
    assert by_name["config"].status == doctor.PASS
    assert "config_dir" in by_name["config"].extra
    assert "state_dir" in by_name["config"].extra
    # python + dependency + writability + telemetry + registry + pricing all present
    for required in ("python", "dependencies", "config_dir", "state_dir", "telemetry",
                     "registry", "pricing"):
        assert required in by_name, f"missing doctor check: {required}"


def test_doctor_config_parse_failure_is_the_only_hard_fail(monkeypatch):
    def _boom():
        raise ValueError("bad config.yaml")

    monkeypatch.setattr(doctor._config, "load", _boom)
    checks, exit_code = doctor.run_doctor()
    assert exit_code == 1
    cfg = next(c for c in checks if c.name == "config")
    assert cfg.status == doctor.FAIL


def test_doctor_pricing_age_is_a_warn_not_a_fail(monkeypatch):
    # A stale feed must WARN, never fail the whole preflight.
    from datetime import date

    from cheapskate.econ import pricing as _pricing

    class _Snap:
        def newest_as_of(self):
            return date(2000, 1, 1)  # ancient

    monkeypatch.setattr(_pricing, "load_pricing", lambda *_a, **_k: _Snap())
    check = doctor._check_pricing(doctor._config.load())
    assert check.status == doctor.WARN
    assert "old" in check.detail


def test_render_table_has_a_verdict_line():
    checks = [
        doctor.Check("config", doctor.PASS, "ok"),
        doctor.Check("engine:ollama", doctor.WARN, "not found"),
    ]
    table = doctor.render_table(checks)
    assert "PASS" in table
    assert "1 pass, 1 warn, 0 fail" in table


def test_render_table_flags_a_fail():
    checks = [doctor.Check("config", doctor.FAIL, "bad")]
    table = doctor.render_table(checks)
    assert table.splitlines()[-1].startswith("FAIL")


def test_engine_absence_never_raises(monkeypatch):
    # even if a probe itself blows up in an unexpected way, doctor degrades.
    import httpx

    def _weird(*_a, **_k):
        raise RuntimeError("something odd")

    monkeypatch.setattr(httpx, "get", _weird)
    monkeypatch.setattr(doctor.shutil, "which", lambda *_a, **_k: None)
    checks, exit_code = doctor.run_doctor()
    assert exit_code == 0
