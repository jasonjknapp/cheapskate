# SPDX-License-Identifier: Apache-2.0
"""Pins key auth + both admission gates. No network, no live servers, no sleeps."""

from __future__ import annotations

import asyncio

import pytest

from cheapskate.broker.gates import (
    CLASS_PRIORITY,
    DEFAULT_CLASS,
    ModelAwareGate,
    PriorityGate,
    class_for_key,
    genkey,
    load_keys,
    priority_of,
)


# ── key auth + QoS classes ──────────────────────────────────────────────────


def test_generic_classes_only():
    # No personal key-class names survived the extraction.
    assert set(CLASS_PRIORITY) == {"interactive", "background"}
    assert CLASS_PRIORITY["interactive"] < CLASS_PRIORITY["background"]


def test_genkey_registers_and_persists(tmp_path):
    path = tmp_path / "keys.json"
    key = genkey("alice", "interactive", path=path)
    assert key.startswith("sk-local-")
    keys = load_keys(path)
    assert keys[key] == {"user": "alice", "class": "interactive"}


def test_genkey_rejects_unknown_class(tmp_path):
    with pytest.raises(ValueError):
        genkey("bob", "vip", path=tmp_path / "k.json")


def test_key_file_is_mode_600(tmp_path):
    path = tmp_path / "keys.json"
    genkey("alice", "background", path=path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_class_for_key_unknown_key_is_rejected():
    assert class_for_key("nope", {}) is None
    assert class_for_key("", {"": {}}) is None


def test_class_for_key_defaults_unknown_class():
    keys = {"k": {"user": "u", "class": "made-up"}}
    assert class_for_key("k", keys) == DEFAULT_CLASS


def test_priority_of_default():
    assert priority_of(None) == CLASS_PRIORITY[DEFAULT_CLASS]
    assert priority_of("interactive") == 0


# ── PriorityGate ────────────────────────────────────────────────────────────


def test_priority_gate_serializes_and_returns_queued_time():
    async def run():
        gate = PriorityGate()
        q1 = await gate.acquire(0)
        assert q1 >= 0
        assert gate._busy is True

        # A second acquire cannot proceed while busy.
        second = asyncio.create_task(gate.acquire(0))
        await asyncio.sleep(0)
        assert not second.done()  # blocked behind the running one

        await gate.release()
        q2 = await asyncio.wait_for(second, timeout=1)
        assert q2 >= 0
        await gate.release()

    asyncio.run(run())


def test_priority_gate_admits_higher_class_first():
    async def run():
        gate = PriorityGate()
        # Occupy the gate.
        await gate.acquire(0)

        order = []

        async def waiter(prio, label):
            await gate.acquire(prio)
            order.append(label)

        # Enqueue background first, then interactive — interactive must win.
        bg = asyncio.create_task(waiter(1, "background"))
        await asyncio.sleep(0)
        inter = asyncio.create_task(waiter(0, "interactive"))
        await asyncio.sleep(0)

        await gate.release()  # let the first waiter in
        await asyncio.sleep(0)
        await gate.release()  # let the second waiter in
        await asyncio.wait_for(asyncio.gather(bg, inter), timeout=1)
        await gate.release()

        assert order == ["interactive", "background"]

    asyncio.run(run())


def test_priority_gate_cancelled_waiter_leaves_queue():
    async def run():
        gate = PriorityGate()
        await gate.acquire(0)  # busy

        waiter = asyncio.create_task(gate.acquire(0))
        await asyncio.sleep(0)
        assert gate.depths()["interactive"] == 1  # one waiter queued

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        # The cancelled token must have left the heap.
        assert sum(gate.depths().values()) == 0

        # A fresh waiter can still be admitted (queue not wedged).
        await gate.release()
        q = await asyncio.wait_for(gate.acquire(0), timeout=1)
        assert q >= 0
        await gate.release()

    asyncio.run(run())


# ── ModelAwareGate._pick (pure decision) ────────────────────────────────────


def _ticket(gate, model, size, backend, priority):
    from cheapskate.broker.gates import _Ticket

    t = _Ticket(model, size, backend, priority, next(gate._counter))
    gate._waiters.append(t)
    return t


def test_model_aware_coresidence_no_swap():
    gate = ModelAwareGate(budget_gb=100)
    gate._resident = {"a": {"size": 30, "ts": 0, "big": False}}
    t = _ticket(gate, "b", 30, "ollama", 1)  # 30 + 30 <= 100 → co-resident
    picked, evict = gate._pick()
    assert picked is t
    assert evict == []


def test_model_aware_same_model_always_admits():
    gate = ModelAwareGate(budget_gb=10)  # tiny budget
    gate._resident = {"a": {"size": 40, "ts": 0, "big": True}}
    t = _ticket(gate, "a", 40, "ollama", 1)  # same model already resident
    picked, evict = gate._pick()
    assert picked is t
    assert evict == []


def test_model_aware_mlx_generation_owns_the_machine():
    from cheapskate.broker.gates import _Ticket

    gate = ModelAwareGate(budget_gb=100)
    active = _Ticket("big", 40, "mlx", 0, next(gate._counter))
    gate._active.append(active)
    _ticket(gate, "b", 10, "ollama", 0)
    picked, _ = gate._pick()
    assert picked is None  # nothing admitted while an MLX gen is in flight


def test_model_aware_mlx_evicts_everything():
    gate = ModelAwareGate(budget_gb=100)
    gate._resident = {
        "a": {"size": 20, "ts": 0, "big": False},
        "b": {"size": 20, "ts": 1, "big": False},
    }
    t = _ticket(gate, "vision", 40, "mlx", 0)
    picked, evict = gate._pick()
    assert picked is t
    assert set(evict) == {"a", "b"}  # exclusive load de-loads everything


def test_model_aware_ollama_over_budget_evicts_lru():
    gate = ModelAwareGate(budget_gb=60)
    gate._resident = {
        "old": {"size": 40, "ts": 0, "big": True},
        "new": {"size": 40, "ts": 5, "big": True},
    }
    t = _ticket(gate, "want", 40, "ollama", 0)  # needs 40, must evict to fit 60
    picked, evict = gate._pick()
    assert picked is t
    assert evict[0] == "old"  # LRU (oldest ts) goes first


def test_model_aware_hysteresis_blocks_young_big_swap():
    gate = ModelAwareGate(budget_gb=40, clock=lambda: 10.0)
    # A big model loaded at ts=0, now=10 → 10s < 60s residency: still young.
    gate._resident = {"young": {"size": 40, "ts": 0, "big": True}}
    # A background request for a DIFFERENT model would force a swap → blocked.
    _ticket(gate, "other", 40, "ollama", 1)
    picked, _ = gate._pick()
    assert picked is None


def test_model_aware_hysteresis_class0_override_when_queue_empty():
    gate = ModelAwareGate(budget_gb=40, clock=lambda: 10.0)
    gate._resident = {"young": {"size": 40, "ts": 0, "big": True}}
    # An interactive (class-0) request, and NObody queued for the young model →
    # the override lets the swap through.
    t = _ticket(gate, "other", 40, "ollama", 0)
    picked, evict = gate._pick()
    assert picked is t
    assert "young" in evict
