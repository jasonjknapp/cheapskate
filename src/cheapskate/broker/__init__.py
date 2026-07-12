# SPDX-License-Identifier: Apache-2.0
"""The broker: one FastAPI door that owns all local model lifecycle + admission.

A single admission queue serializes every generation through the same memory
accounting and one-large-model discipline the backend layer encodes, so two
callers can never race two big models onto the accelerator at once. The broker
is a thin shell over the backend lifecycle; it never reimplements it.

The testable CORE — key/class auth, the priority gate, the capacity decision,
model resolution, telemetry — is plain stdlib and importable without FastAPI.
``build_app`` / ``serve`` lazily import FastAPI + httpx + uvicorn.
"""

from __future__ import annotations

from .capacity import capacity_decision, memory_snapshot
from .gates import (
    CLASS_PRIORITY,
    DEFAULT_CLASS,
    ModelAwareGate,
    PriorityGate,
    class_for_key,
    genkey,
    load_keys,
    priority_of,
)

__all__ = [
    "capacity_decision",
    "memory_snapshot",
    "CLASS_PRIORITY",
    "DEFAULT_CLASS",
    "ModelAwareGate",
    "PriorityGate",
    "class_for_key",
    "genkey",
    "load_keys",
    "priority_of",
]
