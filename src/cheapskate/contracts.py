# SPDX-License-Identifier: Apache-2.0
"""Model-independent job contracts and failure classification.

Jobs describe what they need; fleet policy decides which model can provide it.
The distinction matters operationally: a responsive model returning the wrong
shape is incompatible with that job, not unavailable machine-wide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import FrozenSet, Iterable


class FailureKind(StrEnum):
    TRANSPORT = "transport"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    SCHEMA = "schema"
    SAFETY = "safety"
    QUALITY = "quality"
    SOURCE_DATA = "source_data"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class JobContract:
    """Stable requirements for one job, independent of a concrete model id."""

    job_id: str
    role: str
    output_mode: str = "text"
    required_capabilities: FrozenSet[str] = field(default_factory=frozenset)
    repair_attempts: int = 1
    deadline_s: float | None = None
    bounded_late_s: float = 0
    quality_floor: float = 1.0
    privacy: str = "never_cloud"

    def __post_init__(self) -> None:
        object.__setattr__(self, "required_capabilities", frozenset(self.required_capabilities))
        if self.repair_attempts < 0:
            raise ValueError("repair_attempts must be >= 0")
        if not 0 <= self.quality_floor <= 1:
            raise ValueError("quality_floor must be between 0 and 1")
        if self.privacy not in {"never_cloud", "cloud_allowed"}:
            raise ValueError("privacy must be 'never_cloud' or 'cloud_allowed'")

    def accepts_capabilities(self, capabilities: Iterable[str]) -> bool:
        return self.required_capabilities.issubset(set(capabilities))


def classify_failure(error: BaseException | str) -> FailureKind:
    """Best-effort operational taxonomy used for recovery and notifications."""

    if isinstance(error, TimeoutError):
        return FailureKind.TIMEOUT
    name = type(error).__name__.lower() if isinstance(error, BaseException) else ""
    text = str(error).lower()
    if "timeout" in name or "timed out" in text or "timeout" in text:
        return FailureKind.TIMEOUT
    if any(token in text for token in ("schema", "valid json", "json object", "response shape")):
        return FailureKind.SCHEMA
    if any(token in text for token in ("safety", "unsafe", "policy rail", "rail rejected")):
        return FailureKind.SAFETY
    if any(token in text for token in ("source data", "missing input", "upstream data")):
        return FailureKind.SOURCE_DATA
    if any(token in text for token in ("quality", "acceptance criteria", "verify_failed")):
        return FailureKind.QUALITY
    if any(token in text for token in ("incompatible", "unsupported", "capability")):
        return FailureKind.INCOMPATIBLE
    if any(token in name for token in ("connect", "network", "transport")):
        return FailureKind.TRANSPORT
    if any(token in text for token in ("unreachable", "connection", "http 502", "http 503")):
        return FailureKind.TRANSPORT
    if any(token in text for token in ("not found", "unavailable", "no model", "404")):
        return FailureKind.UNAVAILABLE
    return FailureKind.UNKNOWN
