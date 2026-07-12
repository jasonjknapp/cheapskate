# SPDX-License-Identifier: Apache-2.0
"""Content-free telemetry: one JSON line per event to ``state_dir()/telemetry.jsonl``.

This is the raw feed the future econ engine consumes, so the fields are chosen
to price a route: model, backend, machine_id, task_type, user, route, duration,
token *counts*, retries, escalation, ok, error kind. Counts and lengths only —
prompt/output/content/text are NEVER written, enforced by construction (a field
with any of those names is dropped and the call fails loudly under assertions).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config as _config
from . import paths

# Field names that would carry raw content. None of these may ever be written.
_FORBIDDEN_FIELDS = frozenset({"prompt", "output", "content", "text", "messages", "payload"})

# Fields the future econ engine relies on — documented here as the schema of record.
_KNOWN_FIELDS = frozenset(
    {
        "model",
        "backend",
        "machine_id",
        "task_type",
        "user",
        "route",  # local | cloud | refused
        "duration_s",
        "tokens_in",
        "tokens_out",
        "retries",
        "escalated",
        "ok",
        "error_kind",
    }
)

_TELEMETRY_MAX_BYTES = 10 * 1024 * 1024  # rotate to .1 past 10 MB

_cached_machine_id: str | None = None


def _telemetry_path() -> Path:
    return paths.state_dir() / "telemetry.jsonl"


def _machine_id() -> str:
    global _cached_machine_id
    if _cached_machine_id is None:
        _cached_machine_id = _config.load().machine.machine_id
    return _cached_machine_id


def _scrub(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop any forbidden (content-bearing) field. The assertion pins the
    invariant so a caller that tries to log content fails loudly in tests /
    debug rather than silently leaking. In production (``-O``) the assert is
    stripped but the filter still drops the field, so content still never lands."""
    offenders = _FORBIDDEN_FIELDS & set(fields)
    assert not offenders, f"telemetry is content-free; refused fields: {sorted(offenders)}"
    return {k: v for k, v in fields.items() if k not in _FORBIDDEN_FIELDS}


def _rotate_if_large(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _TELEMETRY_MAX_BYTES:
            os.replace(path, path.parent / (path.name + ".1"))
    except OSError:
        pass


def log_event(kind: str, **fields: Any) -> None:
    """Append one content-free telemetry event. Never raises (telemetry must
    never break a completion). Every event carries ``ts`` (UTC ISO) and
    ``machine_id`` on top of ``kind`` and the caller's counts/lengths."""
    if os.environ.get("CHEAPSKATE_TELEMETRY_OFF") == "1":
        return
    try:
        safe = _scrub(fields)
        record: dict[str, Any] = {
            "ts": _utc_iso(),
            "kind": kind,
            "machine_id": _machine_id(),
            **safe,
        }
        path = _telemetry_path()
        _rotate_if_large(path)
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except AssertionError:
        # A content-bearing field is a programming error — re-raise so it is
        # caught in tests and never silently swallowed.
        raise
    except Exception:  # noqa: BLE001 — telemetry must never break a completion
        pass


def _utc_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
