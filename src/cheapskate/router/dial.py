# SPDX-License-Identifier: Apache-2.0
"""The spend dial: how aggressively to prefer local over cloud.

Levels:
  0  cloud-first   — reach for the cloud model by default
  1  balanced      — local where the route floor allows, cloud otherwise
  2  local-first   — prefer local; a sub-dial tunes intensity (lite|std|max)
  3  local-only    — never leave the machine

The dial is read FRESH from ``state_dir()/<state_file>`` on every call — never
cached. That is deliberate: an operator flips the dial by writing the file and
the very next routing decision must see it, with no process restart.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths
from ..config import Config

# Sub-dial only carries meaning at level 2.
_SUB_DIALS = ("lite", "std", "max")
_MIN_LEVEL, _MAX_LEVEL = 0, 3


def parse_dial(raw: str, default_level: int = 2, default_sub_dial: str = "std") -> tuple[int, str | None]:
    """Parse a dial string into ``(level, sub_dial)``.

    Accepts ``"2:max"`` → ``(2, "max")``, ``"1"`` → ``(1, None)``, and tolerates
    a leading label like ``"level-2:std"``. Junk or empty ⇒ the supplied default.
    The sub-dial is only meaningful at level 2; at level 2 an absent/invalid
    sub-dial normalizes to ``"std"``, and at other levels it is dropped.
    """
    raw = (raw or "").strip()
    if not raw:
        return (default_level, default_sub_dial if default_level == 2 else None)
    body = raw
    if "-" in body and not body.lstrip("-")[:1].isdigit():
        # strip a leading non-numeric label segment ("level-2:std" → "2:std")
        body = body.split("-", 1)[1]
    sub: str | None = None
    if ":" in body:
        body, sub = body.split(":", 1)
        sub = sub.strip().lower() or None
    try:
        level = int(body.strip())
    except ValueError:
        return (default_level, default_sub_dial if default_level == 2 else None)
    if level < _MIN_LEVEL or level > _MAX_LEVEL:
        return (default_level, default_sub_dial if default_level == 2 else None)
    if level == 2:
        if sub not in _SUB_DIALS:
            sub = default_sub_dial if default_sub_dial in _SUB_DIALS else "std"
    else:
        sub = None
    return (level, sub)


def read_dial(config: Config, path: Path | None = None) -> tuple[int, str | None]:
    """Read the current dial FRESH from the state file. Missing/unreadable file
    ⇒ the config's shipped default. Never cached, never raises."""
    state_path = path if path is not None else (paths.state_dir() / config.dial.state_file)
    default_level = config.dial.default_level
    default_sub = config.dial.default_sub_dial
    try:
        raw = Path(state_path).read_text()
    except OSError:
        return parse_dial("", default_level, default_sub)
    return parse_dial(raw, default_level, default_sub)


def format_dial(level: int, sub_dial: str | None) -> str:
    """Render a dial tuple back to its canonical string (``"2:max"``, ``"1"``)."""
    return f"{level}:{sub_dial}" if (level == 2 and sub_dial) else str(level)


def write_dial(config: Config, level: int, sub_dial: str | None = None, path: Path | None = None) -> tuple[int, str | None]:
    """Set the dial by writing the state file (round-tripped through parsing so
    the persisted value is always canonical). Returns the effective tuple."""
    level, sub_dial = parse_dial(
        format_dial(level, sub_dial), config.dial.default_level, config.dial.default_sub_dial
    )
    state_path = Path(path) if path is not None else (paths.state_dir() / config.dial.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(format_dial(level, sub_dial) + "\n")
    return (level, sub_dial)
