# SPDX-License-Identifier: Apache-2.0
"""Router: the spend dial, the pure routing decision (with symmetric
never_local / never_cloud fail-closed classes), and the verify-and-repair
task primitive.
"""

from __future__ import annotations

from .dial import format_dial, parse_dial, read_dial, write_dial
from .routes import (
    NeverCloud,
    NeverLocal,
    route_decision,
)
from .task import run

__all__ = [
    "parse_dial",
    "read_dial",
    "write_dial",
    "format_dial",
    "route_decision",
    "NeverLocal",
    "NeverCloud",
    "run",
]
