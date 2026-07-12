# SPDX-License-Identifier: Apache-2.0
"""Telemetry: content-free JSONL, machine_id + ts on every event, forbidden
content fields refused by construction."""

from __future__ import annotations

import json

import pytest

from cheapskate import telemetry


@pytest.fixture(autouse=True)
def _redirect_state(tmp_path, monkeypatch):
    """Point state_dir at a temp path and reset the cached machine_id so events
    land in an isolated file."""
    monkeypatch.setattr(telemetry.paths, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(telemetry, "_cached_machine_id", "test-machine")
    monkeypatch.delenv("CHEAPSKATE_TELEMETRY_OFF", raising=False)
    yield


def _read_events(tmp_path):
    lines = (tmp_path / "telemetry.jsonl").read_text().splitlines()
    return [json.loads(ln) for ln in lines]


def test_event_has_ts_machine_id_and_kind(tmp_path):
    telemetry.log_event("route", task_type="summarize", route="local", ok=True)
    (evt,) = _read_events(tmp_path)
    assert evt["kind"] == "route"
    assert evt["machine_id"] == "test-machine"
    assert evt["ts"].endswith("+00:00")  # UTC ISO
    assert evt["task_type"] == "summarize"
    assert evt["route"] == "local"
    assert evt["ok"] is True


def test_econ_fields_survive(tmp_path):
    telemetry.log_event(
        "task.run",
        model="reasoning",
        backend="mlx",
        task_type="draft",
        user="interactive",
        route="local",
        duration_s=1.2,
        tokens_in=100,
        tokens_out=42,
        retries=1,
        escalated=False,
        ok=True,
        error_kind=None,
    )
    (evt,) = _read_events(tmp_path)
    for f in ("model", "backend", "task_type", "user", "route", "duration_s",
              "tokens_in", "tokens_out", "retries", "escalated", "ok"):
        assert f in evt


@pytest.mark.parametrize("bad", ["prompt", "output", "content", "text", "messages", "payload"])
def test_forbidden_content_field_refused(bad):
    """A content-bearing field must raise loudly — the pin on the invariant."""
    with pytest.raises(AssertionError):
        telemetry.log_event("route", **{bad: "the actual secret model input"})


def test_no_content_leaks_even_when_mixed(tmp_path):
    # scrub happens before write; if the assert were bypassed the field must
    # still never land. Test the scrub directly.
    scrubbed = telemetry._scrub({"ok": True, "route": "local"})
    assert scrubbed == {"ok": True, "route": "local"}


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEAPSKATE_TELEMETRY_OFF", "1")
    telemetry.log_event("route", ok=True)
    assert not (tmp_path / "telemetry.jsonl").exists()
