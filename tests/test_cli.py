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
