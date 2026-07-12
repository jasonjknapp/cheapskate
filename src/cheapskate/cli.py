# SPDX-License-Identifier: Apache-2.0
"""Cheapskate command line: dial · models · task · serve · doctor · econ · report.

Thin by design, every subcommand is a few lines that shell out to the module
that owns the logic. The CLI parses args and prints; it decides nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import config as _config
from . import paths
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
    # Import specific names: ``backends.__init__`` re-exports the ``resolve``
    # FUNCTION under the ``resolve`` name, which shadows the submodule attribute.
    from .backends.resolve import _get, _roles, role_sources

    cfg = _config.load()
    reg = _registry.load()
    # The EFFECTIVE, precedence-layered table: config > registry > shipped
    # defaults (so a fresh install shows suggested roles instead of blank). The
    # ``source`` marker tells the reader which entries are real vs suggested.
    roles = _roles(cfg)
    sources = role_sources(cfg)
    out = {
        role: {
            "model": _get(rc, "model"),
            "backend": _get(rc, "backend"),
            "approx_gb": _get(rc, "approx_gb"),
            "fallback": _get(rc, "fallback"),
            "rollback": _get(rc, "rollback", []),
            "quarantine": _get(rc, "quarantine", []),
            "source": sources.get(role, "default"),
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
    except _task.CloudUnavailable as exc:
        _print({"task_type": args.task_type, "route": "refused", "class": "cloud_unavailable", "reason": str(exc)})
        return 2
    except _task.LocalUnavailable as exc:
        _print({"task_type": args.task_type, "route": "refused", "class": "local_unavailable", "reason": str(exc)})
        return 2
    # No output means the model produced nothing usable, whether the run failed
    # (broker down / model errored on every attempt) or "succeeded" on an empty
    # answer. Either way it is not a result the user can use: surface it as an
    # error with an actionable hint and a non-zero exit, never a silent null.
    if result.get("output") is None:
        err = result.get("error_kind") or "empty_output"
        hint = (
            "is the broker running? start it with `cheapskate serve`"
            if err in ("CheapskateUnavailable", "ConnectError", "ConnectionError",
                       "ConnectTimeout", "ReadTimeout")
            else "the model returned no usable output"
        )
        _print({**result, "error": f"local task produced no output ({err}); {hint}"})
        return 1
    _print(result)
    return 0


# ── serve ────────────────────────────────────────────────────────────────────


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = _config.load()
    try:
        from .broker import app as broker_app
    except Exception as exc:  # noqa: BLE001, broker deps optional at v0.1
        _print({"error": f"broker app unavailable: {type(exc).__name__}: {exc}"})
        return 1
    serve = getattr(broker_app, "serve", None)
    if serve is None:
        _print({"error": "broker app has no serve() entry point"})
        return 1
    serve(cfg)
    return 0


# ── mcp ──────────────────────────────────────────────────────────────────────


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Run the stdio MCP server (needs the 'mcp' extra). Exposes run_task and
    econ_report to any MCP client (a code assistant, an agent CLI)."""
    cfg = _config.load()
    try:
        from . import mcp_server
    except Exception as exc:  # noqa: BLE001
        _print({"error": f"mcp server unavailable: {type(exc).__name__}: {exc}"})
        return 1
    try:
        mcp_server.serve(cfg)
    except ImportError as exc:
        _print({"error": str(exc)})
        return 1
    return 0


# ── eval ─────────────────────────────────────────────────────────────────────


def _cmd_eval(args: argparse.Namespace) -> int:
    """Run the shipped deterministic eval set for a role (or the whole set).

    Injected mode (default) runs a canned offline ``complete``, no model, no
    network, so a stranger can prove the harness scores green from a bare clone
    and CI can gate on it. ``--live`` binds the real broker client so the same
    set gates an actual model. The exit code is the gate: 0 iff every CRITICAL
    task passed.
    """
    from .evals import run_eval_set
    from .evals.runner import perfect_complete

    if args.live:
        from . import client as _client

        cfg = _config.load()

        def complete(prompt: str, *, system=None, role=None, model=None) -> str:
            return _client.complete(
                prompt, system=system, role=role, model=model, config=cfg
            )["text"]
    else:
        complete = perfect_complete()

    summary = run_eval_set(complete, role=args.role, model=args.model)
    mode = "live" if args.live else "injected"
    gate_ok = summary["critical_passed"] == summary["critical_total"]
    _print(
        {
            "mode": mode,
            "role": args.role or "all",
            "model": args.model,
            "pass_rate": round(summary["pass_rate"], 3),
            "passed": summary["passed"],
            "total": summary["total"],
            "critical_passed": summary["critical_passed"],
            "critical_total": summary["critical_total"],
            "gate": "PASS" if gate_ok else "FAIL",
            "results": summary["results"],
        }
    )
    return 0 if gate_ok else 1


# ── doctor ───────────────────────────────────────────────────────────────────


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Full preflight: config parses + effective paths; dirs writable; python/dep
    versions; registry roles; serving engines present/reachable (report, never
    fail on a bare machine); telemetry writable; pricing feed age.

    Prints a PASS/WARN/FAIL table (or ``--json`` for the raw checks). Exits 0
    unless something genuinely broke, a missing serving engine is a WARN, so a
    fresh clone with nothing running still exits 0."""
    from . import doctor as _doctor

    checks, exit_code = _doctor.run_doctor()
    if getattr(args, "json", False):
        _print(
            {
                "ok": exit_code == 0,
                "checks": [
                    {"check": c.name, "status": c.status, "detail": c.detail, **c.extra}
                    for c in checks
                ],
            }
        )
    else:
        print(_doctor.render_table(checks))
    return exit_code


# ── econ ─────────────────────────────────────────────────────────────────────


def _cmd_econ(args: argparse.Namespace) -> int:
    """The recommendation table: per-task-type routing recommendation from
    measured telemetry (stay-local | go-cloud | mixed), plus the true $/1M-token
    local-vs-cloud comparison."""
    from .econ import report as _report

    cfg = _config.load()
    bundle = _report.generate(cfg, month=args.month, cloud_ref_model=args.cloud_ref)
    if not bundle.reports:
        print(
            "No telemetry yet for "
            + (args.month or "this month")
            + ", run some tasks, then `cheapskate econ`."
        )
        return 0
    text = _report.render_report(
        bundle.reports,
        bundle.receipts,
        pricing_origin=bundle.pricing_origin,
        staleness=bundle.staleness,
        power=bundle.power,
    )
    print(text)
    return 0


# ── report ───────────────────────────────────────────────────────────────────


def _cmd_report(args: argparse.Namespace) -> int:
    """Monthly receipts. ``--share`` emits a content-free aggregate markdown
    receipt safe to post publicly; otherwise the full human report."""
    from .econ import report as _report

    cfg = _config.load()
    bundle = _report.generate(cfg, month=args.month, cloud_ref_model=args.cloud_ref)

    if args.share:
        print(
            _report.render_share(
                bundle.reports, bundle.receipts, machine_id=cfg.machine.machine_id
            )
        )
        return 0

    if not bundle.reports:
        print(
            "No telemetry yet for "
            + (args.month or "this month")
            + ", nothing to report."
        )
        return 0
    print(
        _report.render_report(
            bundle.reports,
            bundle.receipts,
            pricing_origin=bundle.pricing_origin,
            staleness=bundle.staleness,
            power=bundle.power,
        )
    )
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
    sub.add_parser("mcp", help="run the stdio MCP server (needs the 'mcp' extra)")

    doc = sub.add_parser("doctor", help="full preflight (config, dirs, versions, engines, pricing)")
    doc.add_argument("--json", action="store_true", help="emit raw checks as JSON instead of a table")

    ev = sub.add_parser("eval", help="run the shipped deterministic eval set (quality gate)")
    ev.add_argument("--role", default=None, help="restrict to one role (reasoning|classification|code)")
    ev.add_argument("--model", default=None, help="concrete model tag to pin (live mode)")
    ev.add_argument(
        "--live", action="store_true",
        help="run through the real broker client (default: injected offline mode)",
    )

    e = sub.add_parser("econ", help="per-task-type routing recommendation table")
    e.add_argument("--month", default=None, help="restrict to a month (YYYY-MM)")
    e.add_argument(
        "--cloud-ref", dest="cloud_ref", default="gpt-5.4-mini",
        help="cloud model to price the cloud-equivalent against",
    )

    r = sub.add_parser("report", help="monthly savings receipts")
    r.add_argument(
        "--share", action="store_true",
        help="emit a content-free aggregate markdown receipt (safe to post)",
    )
    r.add_argument("--month", default=None, help="restrict to a month (YYYY-MM)")
    r.add_argument(
        "--cloud-ref", dest="cloud_ref", default="gpt-5.4-mini",
        help="cloud model to price the cloud-equivalent against",
    )
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
    if args.cmd == "mcp":
        return _cmd_mcp(args)
    if args.cmd == "doctor":
        return _cmd_doctor(args)
    if args.cmd == "eval":
        return _cmd_eval(args)
    if args.cmd == "econ":
        return _cmd_econ(args)
    if args.cmd == "report":
        return _cmd_report(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
