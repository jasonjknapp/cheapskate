# SPDX-License-Identifier: Apache-2.0
"""Cheapskate command line: dial · models · task · serve · doctor · report.

Thin by design — every subcommand is a few lines that shell out to the module
that owns the logic. The CLI parses args and prints; it decides nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config as _config
from . import paths
from . import telemetry
from .registry import registry as _registry
from .router import dial as _dial
from .router import routes as _routes
from .router import task as _task


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ── dial ─────────────────────────────────────────────────────────────────────


def _cmd_dial(args: argparse.Namespace) -> int:
    cfg = _config.load()
    if args.dial_cmd == "set":
        level, sub = _dial.parse_dial(
            args.value, cfg.dial.default_level, cfg.dial.default_sub_dial
        )
        level, sub = _dial.write_dial(cfg, level, sub)
        _print({"dial": _dial.format_dial(level, sub), "level": level, "sub_dial": sub})
        return 0
    # show (default)
    level, sub = _dial.read_dial(cfg)
    _print(
        {
            "dial": _dial.format_dial(level, sub),
            "level": level,
            "sub_dial": sub,
            "meaning": _LEVEL_MEANINGS.get(level, "unknown"),
            "state_file": str(paths.state_dir() / cfg.dial.state_file),
        }
    )
    return 0


_LEVEL_MEANINGS = {
    0: "cloud-first",
    1: "balanced",
    2: "local-first (sub-dial lite|std|max)",
    3: "local-only",
}


# ── models ───────────────────────────────────────────────────────────────────


def _cmd_models(args: argparse.Namespace) -> int:
    reg = _registry.load()
    roles = reg.get("roles", {})
    out = {
        role: {
            "model": rc.get("model"),
            "backend": rc.get("backend"),
            "fallback": rc.get("fallback"),
            "rollback": rc.get("rollback", []),
            "quarantine": rc.get("quarantine", []),
        }
        for role, rc in roles.items()
    }
    _print({"roles": out, "protected": sorted(_registry.protected_models(reg))})
    return 0


# ── task ─────────────────────────────────────────────────────────────────────


def _read_arg_or_file(val: str) -> str:
    """A '-' reads stdin; a small existing file path reads its contents; else the
    literal string."""
    from pathlib import Path

    if val == "-":
        return sys.stdin.read()
    path = Path(val)
    if len(val) < 400 and path.exists() and path.is_file():
        return path.read_text()
    return val


def _cmd_task(args: argparse.Namespace) -> int:
    cfg = _config.load()
    criteria = _read_arg_or_file(args.criteria)
    payload = _read_arg_or_file(args.infile)
    try:
        result = _task.run(args.task_type, criteria, payload, cfg)
    except _routes.NeverLocal as exc:
        _print({"task_type": args.task_type, "route": "refused", "class": "never_local", "reason": str(exc)})
        return 2
    except _routes.NeverCloud as exc:
        _print({"task_type": args.task_type, "route": "refused", "class": "never_cloud", "reason": str(exc)})
        return 2
    _print(result)
    return 0


# ── serve ────────────────────────────────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = _config.load()
    try:
        from .broker import app as broker_app
    except Exception as exc:  # noqa: BLE001 — broker deps optional at v0.1
        _print({"error": f"broker app unavailable: {type(exc).__name__}: {exc}"})
        return 1
    serve = getattr(broker_app, "serve", None)
    if serve is None:
        _print({"error": "broker app has no serve() entry point"})
        return 1
    serve(cfg)
    return 0


# ── doctor ───────────────────────────────────────────────────────────────────


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Minimal v0.1 health check: config parses, dirs writable, backends reachable
    (or reported)."""
    checks: list[dict[str, Any]] = []
    ok_all = True

    # config parses
    try:
        cfg = _config.load()
        checks.append({"check": "config", "ok": True})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "config", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        _print({"ok": False, "checks": checks})
        return 1

    # dirs writable
    for name, d in (("config_dir", paths.config_dir()), ("state_dir", paths.state_dir())):
        try:
            probe = d / ".cheapskate-doctor-probe"
            probe.write_text("ok")
            probe.unlink()
            checks.append({"check": name, "ok": True, "path": str(d)})
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            checks.append({"check": name, "ok": False, "path": str(d), "error": str(exc)})

    # telemetry is content-free (writes an event; the writer enforces it)
    try:
        telemetry.log_event("doctor", ok=True)
        checks.append({"check": "telemetry", "ok": True})
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        checks.append({"check": "telemetry", "ok": False, "error": str(exc)})

    # backend endpoints reachable-or-reported (never fails doctor: just reports)
    for name, entry in cfg.backends.items():
        if not entry.enabled or not entry.url:
            checks.append({"check": f"backend:{name}", "ok": None, "note": "disabled or no url"})
            continue
        checks.append({"check": f"backend:{name}", **_probe_url(entry.url)})

    _print({"ok": ok_all, "checks": checks})
    return 0 if ok_all else 1


def _probe_url(url: str) -> dict[str, Any]:
    """Report reachability of a backend URL. Never raises; a down backend is
    reported, not fatal."""
    try:
        import httpx

        resp = httpx.get(url, timeout=2.0)
        return {"ok": True, "reachable": True, "url": url, "status": resp.status_code}
    except Exception as exc:  # noqa: BLE001 — unreachable is reported, not an error
        return {"ok": None, "reachable": False, "url": url, "error": type(exc).__name__}


# ── report ───────────────────────────────────────────────────────────────────


def _cmd_report(args: argparse.Namespace) -> int:
    print("econ engine lands in v0.2 of this build")
    return 0


# ── parser ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cheapskate", description="Route tasks to the cheapest model that passes your bar.")
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("dial", help="show or set the spend dial")
    dsub = d.add_subparsers(dest="dial_cmd")
    dsub.add_parser("show", help="show the current dial (default)")
    dset = dsub.add_parser("set", help="set the dial, e.g. 2:max or 1 or 3")
    dset.add_argument("value")

    m = sub.add_parser("models", help="registry model roles")
    m.add_subparsers(dest="models_cmd").add_parser("list", help="list roles (default)")

    t = sub.add_parser("task", help="run one supervised subtask")
    t.add_argument("run", nargs="?", help="(subcommand slot; only 'run' is defined)")
    t.add_argument("--task-type", dest="task_type", required=True)
    t.add_argument("--criteria", required=True, help="acceptance criteria (string or file path)")
    t.add_argument("--in", dest="infile", default="-", help="input payload (file, or - for stdin)")

    sub.add_parser("serve", help="run the broker daemon (needs broker deps)")
    sub.add_parser("doctor", help="check config, dirs, and backend reachability")
    sub.add_parser("report", help="econ report (stub in v0.1)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "dial":
        return _cmd_dial(args)
    if args.cmd == "models":
        return _cmd_models(args)
    if args.cmd == "task":
        return _cmd_task(args)
    if args.cmd == "serve":
        return _cmd_serve(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "report":
        return _cmd_report(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
