# SPDX-License-Identifier: Apache-2.0
"""Config loader: shipped defaults (as data) deep-merged under a user's
``config_dir()/config.yaml``.

Nothing secret lives here, secrets come from environment variables only. The
model is a pydantic ``Config`` so downstream modules read typed, validated
sections instead of poking at raw dicts.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from . import paths


# ── sub-models ───────────────────────────────────────────────────────────────


class BrokerConfig(BaseModel):
    """The single local-inference gateway. Loopback by default; a non-loopback
    bind is a deliberate opt-in for LAN/tailnet reach."""

    host: str = "127.0.0.1"
    port: int = 4747
    bind_loopback: bool = True
    bind_lan: bool = False
    keys_file: str = "broker-keys.json"
    gate: str = "model-aware"
    consumer_routing: bool = True


class DialConfig(BaseModel):
    """The spend dial. ``level`` is the shipped default when the state file is
    absent; ``sub_dial`` is the intensity sub-dial that only applies at level 2."""

    default_level: int = 2
    default_sub_dial: str = "std"
    state_file: str = "dial"


def _default_machine_id() -> str:
    """Sanitized short hostname, lowercase, alnum/dash only, no domain."""
    host = (socket.gethostname() or "machine").split(".")[0].strip().lower()
    cleaned = "".join(c if (c.isalnum() or c == "-") else "-" for c in host).strip("-")
    return cleaned or "machine"


def _detected_ram_gb() -> float | None:
    """Best-effort total RAM in GB (None if undeterminable, callers fail closed)."""
    try:
        pages = __import__("os").sysconf("SC_PHYS_PAGES")
        page_size = __import__("os").sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / 1e9, 1)
    except (ValueError, OSError, AttributeError):
        return None


class MachineConfig(BaseModel):
    """Identity + RAM budget for this host. ``machine_id`` stamps every telemetry
    event. ``ram_gb`` is detected total RAM (None if undeterminable);
    ``ram_budget_gb`` is an explicit override of the load budget, when unset,
    the budget is ``ram_gb - ram_headroom_gb``, and unknown RAM fails closed."""

    machine_id: str = Field(default_factory=_default_machine_id)
    ram_gb: float | None = Field(default_factory=_detected_ram_gb)
    ram_budget_gb: float | None = None
    ram_headroom_gb: float = 24.0
    disk_headroom_gb: float = 15.0
    # Fetch a selected Ollama model on first use when it is not yet pulled,
    # GATED by the same fail-closed disk/size/RAM budget as model currency. True
    # by default (a fresh install just works); set False on a metered connection
    # to require a manual ``ollama pull`` instead.
    auto_pull: bool = True


class BackendEntry(BaseModel):
    """One serving endpoint. A non-localhost ``url`` IS the multi-machine story:
    a remote backend entry points at another host's cheapskate/serving engine."""

    kind: str  # ollama | mlx | lmstudio | remote | cloud
    url: str | None = None
    enabled: bool = True


class TaskTypeRoute(BaseModel):
    """A generic task-type routing rule. ``route`` is the base intent; the dial
    and never_local/never_cloud classes refine it at decision time.

    route ∈ local-first | cloud-only. (never_local / never_cloud are expressed by
    membership in those config lists, not per-entry here.)"""

    route: str = "local-first"
    role: str = "reasoning"
    min_level: int = 1
    tier: str = "std"  # "safe" stays local even at level 2 lite; "std" needs std|max


class UserQuota(BaseModel):
    """Per-user budget guards. None ⇒ unlimited.

    ``monthly_budget_usd`` is the cloud-spend cap the budget governor watches:
    as month-to-date cloud spend approaches it the governor tightens the dial
    toward local (≥80% one level, ≥95% local-only for that user's routable work).
    """

    daily_requests: int | None = None
    daily_tokens: int | None = None
    monthly_budget_usd: float | None = None


class UserProfile(BaseModel):
    """A named key-holder. ``key_class`` sets broker priority: interactive >
    background. The literal API key is never stored here, it lives in the
    broker keys file (mode 600), referenced by env/secret out of band."""

    key_class: str = "interactive"  # interactive | background
    quota: UserQuota = Field(default_factory=UserQuota)


class ProviderConfig(BaseModel):
    """A cloud model provider, a thin adapter target, OFF by default.

    ``kind`` selects the adapter: ``openai-compat`` drives any OpenAI-compatible
    HTTP API (OpenAI, OpenRouter, a Gemini OpenAI-compat endpoint, …) via
    ``base_url``; ``anthropic`` drives Claude through the Anthropic SDK.

    ``model_map`` maps a router *role* (e.g. ``reasoning``, ``code``) to that
    provider's concrete model id. ``api_key_env`` names the environment variable
    the secret is read from, the key itself NEVER lives in config.yaml or the
    repo (Hard rule 3). ``enabled`` is False by default so a shipped install
    reaches the cloud only after a deliberate opt-in."""

    kind: str = "openai-compat"  # openai-compat | anthropic
    base_url: str | None = None  # required for openai-compat; optional override for anthropic
    model_map: dict[str, str] = Field(default_factory=dict)  # role -> concrete model id
    api_key_env: str | None = None  # name of the env var holding the secret
    enabled: bool = False  # OFF by default, a shipped install never reaches cloud unbidden


class EconConfig(BaseModel):
    """Economics inputs the cost engine needs but cannot measure for free.

    All three default to ``None`` on purpose: an honest tool omits a number it
    cannot know rather than guessing. With ``electricity_usd_per_kwh`` unset the
    report runs in "electricity unknown" mode and reports energy cost as N/A;
    with ``hardware_amortization_usd_per_month`` unset no amortization share is
    added. ``pricing_max_age_days`` bounds how stale bundled cloud prices may be
    before the report warns (warn, never fail)."""

    # $/kWh for local energy cost. None ⇒ "electricity unknown": energy cost is
    # omitted from local $/task rather than fabricated.
    electricity_usd_per_kwh: float | None = None
    # Amortized hardware cost per month, spread across the month's local tasks.
    # None ⇒ no amortization share added.
    hardware_amortization_usd_per_month: float | None = None
    # Bundled pricing.json older than this (by its newest as_of) warns, not fails.
    pricing_max_age_days: int = 14
    # Static fallback watts when powermetrics is unavailable / no-sudo. None ⇒
    # power draw unknown (energy cost omitted even if $/kWh is set).
    watts_estimate: float | None = None


# ── shipped defaults (as data) ───────────────────────────────────────────────


def _default_backends() -> dict[str, BackendEntry]:
    return {
        "ollama": BackendEntry(kind="ollama", url="http://127.0.0.1:11434"),
        "mlx": BackendEntry(kind="mlx", url="http://127.0.0.1:8080"),
        "lmstudio": BackendEntry(kind="lmstudio", url="http://127.0.0.1:1234", enabled=False),
        # A remote entry with a non-localhost URL is the multi-machine story in
        # v0.1, disabled by default; fill in a real host to fan out.
        "remote": BackendEntry(kind="remote", url=None, enabled=False),
        "cloud": BackendEntry(kind="cloud", url=None, enabled=False),
    }


def _default_task_types() -> dict[str, TaskTypeRoute]:
    return {
        "summarize": TaskTypeRoute(role="reasoning", min_level=1, tier="safe"),
        "draft": TaskTypeRoute(role="reasoning", min_level=1, tier="safe"),
        "classify": TaskTypeRoute(role="classification", min_level=1, tier="safe"),
        "extract": TaskTypeRoute(role="reasoning", min_level=1, tier="safe"),
        "review": TaskTypeRoute(role="code", min_level=1, tier="std"),
        "boilerplate": TaskTypeRoute(role="code", min_level=2, tier="std"),
    }


def _default_users() -> dict[str, UserProfile]:
    return {
        "interactive": UserProfile(key_class="interactive"),
        "background": UserProfile(key_class="background"),
    }


# ── top-level model ──────────────────────────────────────────────────────────


class Config(BaseModel):
    """The whole configuration surface. Access typed sections, not raw dicts."""

    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    dial: DialConfig = Field(default_factory=DialConfig)
    econ: EconConfig = Field(default_factory=EconConfig)
    machine: MachineConfig = Field(default_factory=MachineConfig)
    backends: dict[str, BackendEntry] = Field(default_factory=_default_backends)
    task_types: dict[str, TaskTypeRoute] = Field(default_factory=_default_task_types)
    # Fail-closed classes. never_local: must never leave to a local model (no
    # silent cloud fallback either, a NeverLocal refusal). never_cloud: must
    # never leave the machine (hard error if routed cloud). Symmetric guards.
    never_local: list[str] = Field(
        default_factory=lambda: ["financial", "legal", "medical", "credentials"]
    )
    never_cloud: list[str] = Field(default_factory=list)
    users: dict[str, UserProfile] = Field(default_factory=_default_users)
    # Cloud providers, every entry OFF by default (BYO keys via env). A shipped
    # install reaches the cloud only after an operator enables a provider AND
    # sets its api_key_env in the environment.
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


# ── deep-merge + load ────────────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins on scalars;
    dicts merge key-by-key). Neither input is mutated."""
    out = dict(base)
    for key, val in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _config_path() -> Path:
    return paths.config_dir() / "config.yaml"


def load(path: Path | None = None) -> Config:
    """Load the effective config: shipped defaults deep-merged under the user's
    ``config.yaml`` (if present). ``path`` overrides the location (tests)."""
    cfg_path = path if path is not None else _config_path()
    shipped = Config().model_dump()
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"config.yaml must be a mapping, got {type(raw).__name__}")
        merged = _deep_merge(shipped, raw)
    else:
        merged = shipped
    return Config.model_validate(merged)
