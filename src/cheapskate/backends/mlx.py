# SPDX-License-Identifier: Apache-2.0
"""MLX server lifecycle with single-large-model safety.

``mlx_lm.server`` serves ONE model and does NOT unload on a model swap: loading a
second large model keeps the first Metal-resident and OOMs / kernel-panics. The
discipline this module encodes, and MUST preserve exactly:

  1. One large model at a time. Switching models RESTARTS the server (kill →
     launch with the new ``--model``). De-load before load.
  2. Always boot with ``--prompt-cache-size 1`` so batch loops can't accumulate
     Metal buffers to OOM.
  3. Refuse to load a model whose footprint exceeds the RAM budget (fail-closed).
  4. Serialize the whole check-and-launch under a MACHINE-WIDE flock, so two
     callers — or a caller racing a separate consumer — can never double-load.

A running generation is never touched by this module: it only manages loads and
de-loads. Every large-model load/swap on the machine flows through the SAME
lock file so the one-large-model rule holds across every consumer.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from ..paths import state_dir
from .resolve import MLX_HOST, MLX_PORT, LocalUnavailable

# One unified lifecycle lock for EVERY large-model load/swap on this machine.
# The one-large-model rule can only be guaranteed if all callers serialize on
# the SAME flock.
LIFECYCLE_LOCK_NAME = "llm-lifecycle.lock"
MLX_STATE_NAME = "mlx_server.json"

MLX_SERVER_BIN = os.environ.get("CHEAPSKATE_MLX_BIN", "mlx_lm.server")
MLX_LOAD_TIMEOUT = 900  # big MLX models can take minutes to load
MLX_READY_POLL = 3  # seconds between readiness polls
PROMPT_CACHE_SIZE = "1"  # bound the prompt cache (OOM guard)


def _lock_path() -> Path:
    return state_dir() / LIFECYCLE_LOCK_NAME


def _state_path() -> Path:
    return state_dir() / MLX_STATE_NAME


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:  # noqa: BLE001
        print(f"  mlx state write failed: {e}", file=sys.stderr)


def _pid_alive(pid: Any) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM  # alive but not ours
    except Exception:  # noqa: BLE001
        return False


def _pgrep_mlx() -> list[int]:
    """PIDs of resident MLX server processes. [] on any error."""
    try:
        p = subprocess.run(
            ["pgrep", "-f", "mlx_lm.server|mlx_vlm"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return []
    out = []
    for tok in p.stdout.split():
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


def _managed_pids() -> set[int]:
    """PIDs this module recorded in its state file (the ones we may run)."""
    pids: set[int] = set()
    pid = _read_state().get("pid")
    if pid:
        try:
            pids.add(int(pid))
        except (TypeError, ValueError):
            pass
    return pids


def assert_no_foreign_mlx(pgrep: Optional[Callable[[], list[int]]] = None) -> None:
    """Fail-closed guard: refuse to launch if an UNMANAGED MLX server is resident.

    A foreign large-model server would double-load and risk a Metal OOM / kernel
    panic. PIDs recorded in this module's state file are exempt (they are ours).
    ``pgrep`` is injectable for tests. Raises RuntimeError on a foreign server.
    """
    runner = pgrep or _pgrep_mlx
    managed = _managed_pids()
    foreign = [p for p in runner() if p not in managed]
    if foreign:
        raise RuntimeError(
            f"unmanaged MLX server (pid {foreign[0]}) resident — refusing to "
            f"double-load; stop it first"
        )


def mlx_health(port: int = MLX_PORT, timeout: int = 4) -> bool:
    """True if an MLX server answers /v1/models on this port. Never raises."""
    try:
        with urllib.request.urlopen(
            f"http://{MLX_HOST}:{port}/v1/models", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _port_in_use(port: int, host: str = MLX_HOST) -> bool:
    """True if something already LISTENs on host:port. Never raises.

    Used as a spawn preflight: by the time we check, our own MLX server is
    stopped, so a live port means a FOREIGN service.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def stop_mlx(
    *,
    killer: Callable[[int, int], object] = os.killpg,
    getpgid: Callable[[int], int] = os.getpgid,
) -> bool:
    """Stop the running MLX server (de-load). Returns True if anything stopped.

    ``killer`` and ``getpgid`` are injectable so tests never touch real
    processes.
    """
    state = _read_state()
    pid = state.get("pid")
    stopped = False
    if pid and _pid_alive(pid):
        try:
            killer(getpgid(int(pid)), signal.SIGTERM)
            stopped = True
            for _ in range(20):  # up to ~5s for graceful exit
                if not _pid_alive(pid):
                    break
                time.sleep(0.25)
            if _pid_alive(pid):
                killer(getpgid(int(pid)), signal.SIGKILL)
        except Exception as e:  # noqa: BLE001
            print(f"  mlx stop: {e}", file=sys.stderr)
    _write_state({})
    return stopped


def _launch_mlx(model: str, port: int) -> int:
    """Spawn the MLX server detached with the prompt cache bound. Returns the pid."""
    log_path = state_dir() / "mlx_server.log"
    cmd = [
        MLX_SERVER_BIN, "--model", model, "--host", MLX_HOST, "--port", str(port),
        "--prompt-cache-size", PROMPT_CACHE_SIZE, "--log-level", "WARNING",
    ]
    logf = open(log_path, "a")
    logf.write(f"\n=== launch {model} :{port} @ {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")
    logf.flush()
    proc = subprocess.Popen(
        cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
    )  # own process group → killpg works
    return proc.pid


def ensure_mlx(
    model: str,
    *,
    approx_gb: Optional[float] = None,
    port: int = MLX_PORT,
    budget_gb: float,
    evict: Optional[Callable[[float], None]] = None,
    launcher: Optional[Callable[[str, int], int]] = None,
    port_checker: Optional[Callable[[int], bool]] = None,
    foreign_check: Optional[Callable[[], None]] = None,
    binary_exists: Optional[Callable[[], bool]] = None,
    load_timeout: int = MLX_LOAD_TIMEOUT,
) -> str:
    """Ensure the MLX server is serving exactly ``model`` (de-loading any other).

    Enforces: one large model at a time, ``--prompt-cache-size 1``, RAM-budget
    refusal, and flock serialization. Raises :class:`LocalUnavailable` on any
    failure so the caller degrades. Returns the endpoint base URL (which may
    carry an auto-corrected port if the requested one was found foreign-occupied,
    so callers must derive the port from the returned base).

    All side-effecting collaborators are injectable so the decision path is
    unit-testable without touching processes, sockets, or the filesystem beyond
    the state dir:
      * ``evict(needed_gb)`` — de-load co-residents before a large load,
      * ``launcher(model, port) -> pid`` — spawn the server,
      * ``port_checker(port) -> bool`` — is the port foreign-occupied,
      * ``foreign_check()`` — raise if an unmanaged server is resident,
      * ``binary_exists()`` — is the server binary present.
    """
    if approx_gb and approx_gb > budget_gb:
        raise LocalUnavailable(
            f"mlx model {model!r} ~{approx_gb}GB exceeds RAM budget "
            f"({budget_gb}GB); refusing to load (OOM guard)"
        )
    exists = binary_exists or (lambda: _binary_on_path(MLX_SERVER_BIN))
    if not exists():
        raise LocalUnavailable(f"mlx server binary not found ({MLX_SERVER_BIN!r})")

    lock_dir = state_dir()
    lock_dir.mkdir(parents=True, exist_ok=True)
    base = f"http://{MLX_HOST}:{port}"

    # Serialize the whole check-and-launch so two callers can't double-load.
    with open(_lock_path(), "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)

        # Fail-closed if an unmanaged MLX server is resident — our own recorded
        # PID is exempt; a foreign one aborts the load.
        (foreign_check or assert_no_foreign_mlx)()
        # Cross-runtime memory safety: de-load co-residents in other runtimes if
        # the combined footprint would breach the RAM budget.
        if evict is not None:
            evict(float(approx_gb or 0))

        state = _read_state()
        # Reuse only if the SAME model is up and healthy.
        if (
            state.get("model") == model
            and _pid_alive(state.get("pid"))
            and mlx_health(port)
        ):
            return base

        # Different model (or stale/dead) → DE-LOAD before loading (hard rule).
        if state.get("pid"):
            stop_mlx()

        # Spawn preflight: our own server is stopped now, so a still-occupied
        # port is a FOREIGN service. Auto-correct to the MLX default when we can;
        # otherwise refuse to spawn a doomed server.
        in_use = port_checker or _port_in_use
        if in_use(port):
            if port != MLX_PORT and not in_use(MLX_PORT):
                print(
                    f"mlx target port {port} is held by a foreign service; "
                    f"auto-correcting to the mlx default {MLX_PORT}",
                    file=sys.stderr,
                )
                port = MLX_PORT
                base = f"http://{MLX_HOST}:{port}"
            else:
                raise LocalUnavailable(
                    f"refusing to start mlx server for {model!r}: port {port} is "
                    f"held by a foreign service"
                    + ("" if port == MLX_PORT else f" and the mlx default {MLX_PORT} is also busy")
                )

        launch = launcher or _launch_mlx
        pid = launch(model, port)
        # Poll for readiness — a big model load is slow.
        deadline = time.monotonic() + load_timeout
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                raise LocalUnavailable(
                    f"mlx server for {model!r} exited during load — likely OOM or bad model"
                )
            if mlx_health(port):
                _write_state({
                    "pid": pid, "model": model, "port": port,
                    "started_at": time.time(),
                })
                return base
            time.sleep(MLX_READY_POLL)

        # Timed out → clean up so we don't leave a half-loaded server.
        stop_mlx()
        raise LocalUnavailable(f"mlx server for {model!r} not ready in {load_timeout}s")


def _binary_on_path(name: str) -> bool:
    """True if ``name`` is an existing file or resolvable on PATH."""
    if os.path.sep in name:
        return Path(name).exists()
    from shutil import which

    return which(name) is not None
