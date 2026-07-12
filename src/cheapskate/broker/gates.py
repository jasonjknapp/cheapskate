# SPDX-License-Identifier: Apache-2.0
"""API-key auth, QoS classes, and the admission gates.

Keys are named, per-user, and carry a QoS class. Two classes ship:
``interactive`` (higher priority) and ``background``. A higher-priority class
jumps the QUEUE ORDER only — a running generation is NEVER preempted.

Two gates are provided:

  * :class:`PriorityGate` — the proven serial gate: one generation at a time,
    class-ordered admission among waiters. This is the OOM/panic floor.
  * :class:`ModelAwareGate` — an anti-thrash scheduler that adds co-residence,
    affinity batching, and min-residency hysteresis. It is a THROUGHPUT
    optimizer only; memory SAFETY is still enforced downstream by the backend
    lifecycle, so a scheduling misjudgment can only cost a serialization, never
    a panic.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import json
import os
import secrets
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..paths import state_dir

# QoS priority classes — LOWER number = higher priority (admitted first).
CLASS_PRIORITY = {
    "interactive": 0,
    "background": 1,
}
DEFAULT_CLASS = "background"

KEYS_FILE_NAME = "broker-keys.json"


def keys_path(config: Any = None) -> Path:
    """Path to the key registry (under the state dir by default)."""
    override = _broker_setting(config, "keys_file")
    if override:
        p = Path(override)
        return p if p.is_absolute() else state_dir() / p
    return state_dir() / KEYS_FILE_NAME


def _broker_setting(config: Any, key: str) -> Any:
    if config is None:
        return None
    broker = config.get("broker", {}) if isinstance(config, dict) else getattr(config, "broker", None)
    if broker is None:
        return None
    return broker.get(key) if isinstance(broker, dict) else getattr(broker, key, None)


def load_keys(path: Optional[Path] = None, config: Any = None) -> dict:
    """Read the key registry ``{key: {"user": str, "class": str}}``. {} if absent."""
    path = Path(path) if path else keys_path(config)
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — missing/unreadable ⇒ no keys registered
        return {}


def class_for_key(key: str, keys: dict) -> Optional[str]:
    """Resolve a bearer key to its QoS class. Unknown/empty key → None (reject)."""
    if not key:
        return None
    entry = keys.get(key)
    if not entry:
        return None
    cls = entry.get("class", DEFAULT_CLASS)
    return cls if cls in CLASS_PRIORITY else DEFAULT_CLASS


def priority_of(cls: Optional[str]) -> int:
    return CLASS_PRIORITY.get(cls, CLASS_PRIORITY[DEFAULT_CLASS])


def genkey(user: str, cls: str, path: Optional[Path] = None, config: Any = None) -> str:
    """Mint a random key, register it with (user, class), persist it mode-600.

    Returns the new key. Raises ValueError on an unknown QoS class.
    """
    if cls not in CLASS_PRIORITY:
        raise ValueError(f"unknown QoS class {cls!r}; valid: {list(CLASS_PRIORITY)}")
    path = Path(path) if path else keys_path(config)
    keys = load_keys(path)
    key = "sk-local-" + secrets.token_urlsafe(24)
    keys[key] = {"user": user, "class": cls}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


# ── Serial priority gate ─────────────────────────────────────────────────────


class PriorityGate:
    """Serializes generations (one large model at a time — the OOM/panic floor).

    Among waiters, the lowest class-priority number is admitted next (higher QoS
    class jumps the queue). A running generation is NEVER preempted. ``acquire``
    returns the seconds the caller spent queued.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._heap: list[tuple[int, int]] = []  # (priority, seq)
        self._counter = itertools.count()
        self._busy = False

    async def acquire(self, priority: int) -> float:
        async with self._cond:
            token = (priority, next(self._counter))
            heapq.heappush(self._heap, token)
            queued_start = time.monotonic()
            try:
                while self._busy or self._heap[0] != token:
                    await self._cond.wait()
            except BaseException:
                # Client gone while queued — our token must leave the heap or it
                # wedges the whole queue once it reaches the head.
                try:
                    self._heap.remove(token)
                    heapq.heapify(self._heap)
                except ValueError:
                    pass
                self._cond.notify_all()
                raise
            heapq.heappop(self._heap)
            self._busy = True
            return time.monotonic() - queued_start

    async def release(self) -> None:
        async with self._cond:
            self._busy = False
            self._cond.notify_all()

    def depths(self) -> dict:
        """Queue depth per class name (waiters only, not the running one)."""
        inv = {v: k for k, v in CLASS_PRIORITY.items()}
        c = Counter(inv.get(p, str(p)) for p, _ in self._heap)
        return {cls: c.get(cls, 0) for cls in CLASS_PRIORITY}


# ── Anti-thrash model-aware gate ─────────────────────────────────────────────

BIG_MODEL_GB = 30.0  # a "big" model for hysteresis purposes
MIN_RESIDENCY_S = 60.0  # a freshly-loaded big model stays >= 60s before a swap


class _Ticket:
    __slots__ = ("model", "size", "backend", "priority", "seq", "admitted", "queued_s")

    def __init__(self, model, size, backend, priority, seq):
        self.model, self.size, self.backend = model, float(size or 0), backend
        self.priority, self.seq = priority, seq
        self.admitted, self.queued_s = False, 0.0

    @property
    def exclusive(self) -> bool:
        return self.backend in ("mlx", "mlx_vlm")

    @property
    def is_big(self) -> bool:
        return self.size >= BIG_MODEL_GB


class ModelAwareGate:
    """Admission scheduler that beats swap-thrash:

      1. CO-RESIDENCE is the default — Ollama requests whose combined footprint
         fits the RAM budget run CONCURRENTLY; same-model requests always do.
      2. AFFINITY BATCHING — before forcing a model swap, every queued request
         the currently-resident models can satisfy is served first
         (class-ordered).
      3. MIN-RESIDENCY HYSTERESIS — a freshly-loaded big model stays resident
         >= ``min_residency_s`` before an eviction-swap may target it, UNLESS the
         queue for it is empty AND a class-0 (interactive) request needs the swap.
      4. EXCLUSIVITY — an MLX generation runs alone, so nothing else is admitted
         while one is in flight.

    Memory SAFETY is enforced downstream by the backend lifecycle; this is a
    throughput optimizer only. The decision logic (:meth:`_pick`) is pure.
    """

    def __init__(self, budget_gb: float, *, min_residency_s: float = MIN_RESIDENCY_S,
                 clock=time.monotonic) -> None:
        self.budget = float(budget_gb)
        self.min_residency_s = min_residency_s
        self._clock = clock
        self._cond = asyncio.Condition()
        self._counter = itertools.count()
        self._waiters: list[_Ticket] = []  # not yet admitted
        self._active: list[_Ticket] = []  # currently generating
        self._resident: dict[str, dict] = {}  # model -> {"size","ts","big"}
        self._timer_armed = False

    async def acquire_ticket(self, *, model, size, backend, priority) -> _Ticket:
        async with self._cond:
            t = _Ticket(model, size, backend, priority, next(self._counter))
            self._waiters.append(t)
            t0 = self._clock()
            self._schedule()
            try:
                while not t.admitted:
                    await self._cond.wait()
                    self._schedule()
            except BaseException:
                # Cancelled while queued — withdraw the ticket so it can never be
                # admitted as a ghost (admitted-but-never-released).
                try:
                    self._waiters.remove(t)
                except ValueError:
                    pass
                self._schedule()
                raise
            self._active.append(t)
            t.queued_s = self._clock() - t0
            return t

    async def release(self, ticket: _Ticket) -> None:
        async with self._cond:
            try:
                self._active.remove(ticket)
            except ValueError:
                pass
            self._schedule()

    # ── scheduling (pure decision in _pick; _schedule applies + notifies) ──

    def _resident_gb(self) -> float:
        return sum(r["size"] for r in self._resident.values())

    def _mark_resident(self, t: _Ticket) -> None:
        self._resident[t.model] = {"size": t.size, "ts": self._clock(), "big": t.is_big}

    def _pick(self):
        """Return the next waiter to admit + models to evict for it, or
        (None, []). Pure w.r.t. current state; no mutation."""
        if any(a.exclusive for a in self._active):
            return None, []  # an MLX gen owns the machine
        pending = sorted(
            (w for w in self._waiters if not w.admitted),
            key=lambda w: (w.priority, w.seq),
        )
        if not pending:
            return None, []
        # PASS 1 — no swap needed (affinity + co-residence). Ollama only.
        rgb = self._resident_gb()
        for w in pending:
            if w.exclusive:
                continue
            if w.model in self._resident or (rgb + w.size) <= self.budget:
                return w, []
        # PASS 2 — a swap/exclusive load is needed; only when nothing is active.
        if self._active:
            return None, []
        w = pending[0]
        if self._hysteresis_blocks(w):
            return None, []
        if w.exclusive:
            return w, list(self._resident)  # MLX evicts everything
        # Ollama over budget → evict LRU non-target residents until it fits.
        evict, rem = [], dict(self._resident)
        rem.pop(w.model, None)
        while (sum(r["size"] for r in rem.values()) + w.size) > self.budget and rem:
            lru = min(rem, key=lambda m: rem[m]["ts"])
            evict.append(lru)
            rem.pop(lru)
        return w, evict

    def _hysteresis_blocks(self, w: _Ticket) -> bool:
        now = self._clock()
        young_big = [
            m for m, r in self._resident.items()
            if r["big"] and (now - r["ts"]) < self.min_residency_s
        ]
        if not young_big:
            return False
        wants_young = any(
            (not x.admitted) and x.model in young_big for x in self._waiters
        )
        if w.priority == CLASS_PRIORITY["interactive"] and not wants_young:
            return False  # class-0 override (queue for the young model is empty)
        return True

    def _schedule(self) -> None:
        changed = False
        while True:
            w, evict = self._pick()
            if w is None:
                break
            for m in evict:
                self._resident.pop(m, None)
            self._mark_resident(w)
            w.admitted = True
            try:
                self._waiters.remove(w)
            except ValueError:
                pass
            changed = True
        if changed:
            self._cond.notify_all()
        self._arm_hysteresis_timer()

    def _arm_hysteresis_timer(self) -> None:
        """If a swap is blocked only by hysteresis, wake at the expiry so the
        waiter isn't stranded when the machine is otherwise idle."""
        if self._timer_armed or self._active:
            return
        pending = [w for w in self._waiters if not w.admitted]
        if not pending:
            return
        now = self._clock()
        young = [
            self.min_residency_s - (now - r["ts"])
            for r in self._resident.values()
            if r["big"] and (now - r["ts"]) < self.min_residency_s
        ]
        if not young:
            return
        delay = max(0.01, min(young))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer_armed = True
        loop.call_later(delay, lambda: asyncio.ensure_future(self._wake()))

    async def _wake(self) -> None:
        async with self._cond:
            self._timer_armed = False
            self._schedule()

    def depths(self) -> dict:
        c: Counter = Counter()
        inv = {v: k for k, v in CLASS_PRIORITY.items()}
        for w in self._waiters:
            if not w.admitted:
                c[inv.get(w.priority, str(w.priority))] += 1
        return {cls: c.get(cls, 0) for cls in CLASS_PRIORITY}

    @property
    def _busy(self) -> bool:
        return bool(self._active)
