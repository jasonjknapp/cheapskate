# SPDX-License-Identifier: Apache-2.0
"""``cheapskate doctor``: a self-contained preflight that runs on a bare machine.

The contract is: it NEVER crashes on a stranger's machine and it NEVER fails just
because a serving engine is absent. Missing engines, unreachable endpoints, and a
stale pricing feed are WARNINGs, not errors — a fresh clone with nothing running
should still exit 0 with a clear PASS/WARN table. Only a genuinely broken
install (config won't parse, a required dir isn't writable) is a hard FAIL.

Each check returns a :class:`Check` with a status of ``pass`` / ``warn`` /
``fail``. The overall exit code is 0 unless some check is ``fail``. Every check
is defensive: any unexpected exception inside a check degrades to a WARN with the
exception kind, never a traceback out of the process.
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import config as _config
from . import paths

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def run_doctor() -> tuple[list[Check], int]:
    """Run every check. Returns ``(checks, exit_code)`` — exit 0 unless a FAIL."""
    checks: list[Check] = []

    cfg, cfg_check = _check_config()
    checks.append(cfg_check)
    checks.extend(_check_versions())
    checks.extend(_check_paths())
    checks.append(_check_telemetry())
    checks.append(_check_registry())
    checks.extend(_check_engines(cfg))
    checks.append(_check_pricing(cfg))

    exit_code = 1 if any(c.status == FAIL for c in checks) else 0
    return checks, exit_code


# ── individual checks ────────────────────────────────────────────────────────


def _check_config() -> tuple[Any, Check]:
    """Config parses + the effective paths it will use. A parse failure is the
    one thing that IS fatal (nothing else can run)."""
    try:
        cfg = _config.load()
    except Exception as exc:  # noqa: BLE001
        return None, Check("config", FAIL, f"config did not parse: {type(exc).__name__}: {exc}")
    return cfg, Check(
        "config",
        PASS,
        "config.yaml parses (shipped defaults if no user file)",
        {
            "config_dir": str(_safe(paths.config_dir)),
            "state_dir": str(_safe(paths.state_dir)),
            "machine_id": getattr(getattr(cfg, "machine", None), "machine_id", "?"),
            "ram_gb": getattr(getattr(cfg, "machine", None), "ram_gb", None),
        },
    )


def _check_versions() -> list[Check]:
    """Python + key dependency versions. A too-old Python is a FAIL (the code
    requires >=3.11); a missing optional dep is a WARN."""
    out: list[Check] = []
    pyver = sys.version_info
    py_ok = pyver >= (3, 11)
    out.append(
        Check(
            "python",
            PASS if py_ok else FAIL,
            f"Python {pyver.major}.{pyver.minor}.{pyver.micro}"
            + ("" if py_ok else " (requires >= 3.11)"),
            {"implementation": platform.python_implementation()},
        )
    )
    deps: dict[str, str] = {}
    missing: list[str] = []
    for mod in ("pydantic", "yaml", "httpx", "fastapi", "starlette", "uvicorn"):
        ver = _module_version(mod)
        if ver is None:
            missing.append(mod)
        else:
            deps[mod] = ver
    # Core deps present ⇒ PASS; a missing serving/broker dep is a WARN (the CLI's
    # read-only commands still work), never a crash.
    status = PASS if not missing else WARN
    detail = "core dependencies present" if not missing else f"missing: {missing}"
    out.append(Check("dependencies", status, detail, {"versions": deps}))
    return out


def _check_paths() -> list[Check]:
    """Config + state dirs exist and are WRITABLE (a probe write/delete)."""
    out: list[Check] = []
    for name, getter in (("config_dir", paths.config_dir), ("state_dir", paths.state_dir)):
        try:
            d = getter()
            probe = d / ".cheapskate-doctor-probe"
            probe.write_text("ok")
            probe.unlink()
            out.append(Check(name, PASS, f"writable: {d}"))
        except Exception as exc:  # noqa: BLE001
            out.append(Check(name, FAIL, f"not writable: {type(exc).__name__}: {exc}"))
    return out


def _check_telemetry() -> Check:
    """Telemetry is writable + content-free by construction (a real write)."""
    try:
        from . import telemetry

        telemetry.log_event("doctor", ok=True)
        return Check("telemetry", PASS, "content-free JSONL writer is functional")
    except AssertionError as exc:
        # A content-bearing field would trip the writer's assertion — that would be
        # a genuine bug, but doctor should report it, not crash.
        return Check("telemetry", FAIL, f"content-free invariant tripped: {exc}")
    except Exception as exc:  # noqa: BLE001
        return Check("telemetry", WARN, f"telemetry write failed: {type(exc).__name__}: {exc}")


def _check_registry() -> Check:
    """Registry is readable + the roles it declares (empty on a fresh clone)."""
    try:
        from .registry import registry as _registry

        reg = _registry.load()
        roles = sorted((reg.get("roles") or {}).keys())
        if not roles:
            return Check(
                "registry",
                WARN,
                "registry.yaml has no roles yet (expected on a fresh clone; add "
                "roles to enable local routing)",
                {"roles": []},
            )
        return Check("registry", PASS, f"{len(roles)} role(s) registered", {"roles": roles})
    except Exception as exc:  # noqa: BLE001
        return Check("registry", WARN, f"registry unreadable: {type(exc).__name__}: {exc}")


def _check_engines(cfg: Any) -> list[Check]:
    """Serving engines: binaries present? endpoints reachable? ALWAYS report,
    NEVER fail — a bare machine with no ollama/mlx is a valid, expected state."""
    out: list[Check] = []

    # binary presence (report only)
    for binary in ("ollama", "mlx_lm.server"):
        found = shutil.which(binary) or shutil.which(binary.split(".")[0])
        if found:
            out.append(Check(f"engine:{binary}", PASS, f"binary found: {found}"))
        else:
            out.append(
                Check(
                    f"engine:{binary}",
                    WARN,
                    "binary not found on PATH (fine on a bare machine; install to "
                    "serve locally)",
                )
            )

    # endpoint reachability (report only)
    backends = getattr(cfg, "backends", None) or {}
    if hasattr(backends, "items"):
        for name, entry in backends.items():
            enabled = getattr(entry, "enabled", True)
            url = getattr(entry, "url", None)
            if not enabled or not url:
                out.append(Check(f"endpoint:{name}", WARN, "disabled or no url configured"))
                continue
            out.append(_probe_endpoint(name, url))
    return out


def _probe_endpoint(name: str, url: str) -> Check:
    """Probe a backend URL. Unreachable is a WARN, never a FAIL."""
    try:
        import httpx

        resp = httpx.get(url, timeout=2.0)
        return Check(
            f"endpoint:{name}", PASS, f"reachable (HTTP {resp.status_code})", {"url": url}
        )
    except Exception as exc:  # noqa: BLE001
        return Check(
            f"endpoint:{name}",
            WARN,
            f"unreachable ({type(exc).__name__}) — not running is fine on a bare machine",
            {"url": url},
        )


def _check_pricing(cfg: Any) -> Check:
    """Bundled cloud-price feed age vs the configured max. Stale is a WARN (the
    weekly CI refresh keeps it current); a missing/unreadable feed is a WARN."""
    max_age = getattr(getattr(cfg, "econ", None), "pricing_max_age_days", 14)
    try:
        from .econ import pricing as _pricing

        snapshot = _pricing.load_pricing()
        newest = snapshot.newest_as_of()
        if newest is None:
            return Check("pricing", WARN, "pricing feed has no dated rows to age-check")
        age_days = (date.today() - newest).days
        if age_days > max_age:
            return Check(
                "pricing",
                WARN,
                f"pricing feed is {age_days}d old (> {max_age}d max); the weekly "
                "refresh action keeps it current",
                {"newest_as_of": newest.isoformat(), "age_days": age_days},
            )
        return Check(
            "pricing",
            PASS,
            f"pricing feed fresh ({age_days}d old, <= {max_age}d)",
            {"newest_as_of": newest.isoformat(), "age_days": age_days},
        )
    except Exception as exc:  # noqa: BLE001
        return Check("pricing", WARN, f"pricing feed unreadable: {type(exc).__name__}: {exc}")


# ── helpers ──────────────────────────────────────────────────────────────────


def _module_version(mod: str) -> str | None:
    try:
        m = __import__(mod)
        return getattr(m, "__version__", "installed")
    except Exception:  # noqa: BLE001
        return None


def _safe(fn: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return "?"


def render_table(checks: list[Check]) -> str:
    """A compact aligned PASS/WARN/FAIL table for humans."""
    width = max((len(c.name) for c in checks), default=8)
    lines = [f"{'STATUS':<6}  {'CHECK':<{width}}  DETAIL"]
    lines.append("-" * (6 + 2 + width + 2 + 40))
    for c in checks:
        lines.append(f"{c.status:<6}  {c.name:<{width}}  {c.detail}")
    n_pass = sum(1 for c in checks if c.status == PASS)
    n_warn = sum(1 for c in checks if c.status == WARN)
    n_fail = sum(1 for c in checks if c.status == FAIL)
    lines.append("-" * (6 + 2 + width + 2 + 40))
    verdict = "FAIL" if n_fail else ("PASS (with warnings)" if n_warn else "PASS")
    lines.append(f"{verdict}: {n_pass} pass, {n_warn} warn, {n_fail} fail")
    return "\n".join(lines)
