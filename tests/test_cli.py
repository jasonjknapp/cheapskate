# SPDX-License-Identifier: Apache-2.0
"""CLI task command: structured refusals, non-zero exit, NO traceback leak (R1).

The XDG state/config dirs are already redirected to a tmp dir by the autouse
``_isolate_state`` conftest fixture, so writing the dial state file here is safe.
"""

from __future__ import annotations

import json

from cheapskate import cli, paths


def _write_dial(value: str) -> None:
    (paths.state_dir() / "dial").write_text(value + "\n")


def test_task_cloud_route_no_provider_refuses_without_traceback(capsys):
    # Dial level 0 forces a cloud route; the default config has no enabled
    # provider → task.run raises CloudUnavailable. The CLI must print a structured
    # refusal (route "refused", class "cloud_unavailable") and exit non-zero, with
    # NO traceback on stdout/stderr.
    _write_dial("0")
    code = cli.main([
        "task", "run",
        "--task-type", "summarize",
        "--criteria", "summarize the text",
        "--in", "some input payload",
    ])
    assert code == 2
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "Traceback" not in combined  # no leaked traceback
    payload = json.loads(out.out)
    assert payload["route"] == "refused"
    assert payload["class"] == "cloud_unavailable"
    assert payload["task_type"] == "summarize"
    assert payload["reason"]  # actionable message present


def test_task_local_failure_surfaces_error_and_nonzero_exit(capsys, monkeypatch):
    # D3: a local route that produces no output (broker down / model errored on
    # every attempt) must NOT print a silent null with exit 0. The CLI surfaces
    # an actionable error, points at `cheapskate serve` for a connection error,
    # and exits non-zero.
    from cheapskate import cli as cli_mod

    def fake_run(task_type, criteria, payload, cfg, **kw):
        return {"task_type": task_type, "route": "local", "role": "reasoning",
                "output": None, "ok": False, "retries": 2, "escalated": True,
                "error_kind": "CheapskateUnavailable"}

    monkeypatch.setattr(cli_mod._task, "run", fake_run)
    code = cli.main([
        "task", "run", "--task-type", "summarize",
        "--criteria", "crit", "--in", "payload",
    ])
    assert code == 1
    out = capsys.readouterr()
    combined = out.out + out.err
    assert "Traceback" not in combined
    payload = json.loads(out.out)
    assert "error" in payload
    assert "cheapskate serve" in payload["error"]  # actionable hint


def test_task_empty_output_even_when_ok_is_error(capsys, monkeypatch):
    # A run that "succeeds" (ok=True) but produced no output is not a usable
    # result: the CLI must surface it as an error with a non-zero exit, not print
    # a silent {"output": null, "ok": true} with exit 0.
    from cheapskate import cli as cli_mod

    def fake_run(task_type, criteria, payload, cfg, **kw):
        return {"task_type": task_type, "route": "local", "role": "reasoning",
                "output": None, "ok": True, "retries": 0, "escalated": False,
                "error_kind": None}

    monkeypatch.setattr(cli_mod._task, "run", fake_run)
    code = cli.main([
        "task", "run", "--task-type", "summarize",
        "--criteria", "crit", "--in", "payload",
    ])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert "error" in payload and "no output" in payload["error"]


def test_task_never_local_refuses_cleanly(capsys):
    # A never_local task type refuses without a traceback and exits non-zero.
    code = cli.main([
        "task", "run",
        "--task-type", "financial",
        "--criteria", "crit",
        "--in", "payload",
    ])
    assert code == 2
    out = capsys.readouterr()
    assert "Traceback" not in (out.out + out.err)
    payload = json.loads(out.out)
    assert payload["route"] == "refused"
    assert payload["class"] == "never_local"


def test_models_list_shows_defaults_with_source_marker(capsys):
    # Fresh state: empty registry, no config.roles → the suggested defaults show
    # up, each marked "source": "default" so they read as suggestions.
    code = cli.main(["models", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    roles = payload["roles"]
    assert "code" in roles and roles["code"]["model"] == "qwen3-coder:30b"
    assert roles["code"]["source"] == "default"
    assert all(rc["source"] == "default" for rc in roles.values())
