# SPDX-License-Identifier: Apache-2.0
"""Resolve a role or model name to a concrete backend spec.

A role is looked up in the registry (``config.backends`` endpoints +
``config.task_types`` style role table); an unknown bare model string falls back
to Ollama for back-compat. The returned :class:`BackendSpec` carries everything
the lifecycle + capacity layers need: the model tag, the serving backend, the
OpenAI-compatible endpoint base, and the approximate RAM footprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Default localhost endpoints per backend. Overridable via config.backends.
DEFAULT_OLLAMA_URL = "http://localhost:11434"
MLX_HOST = "127.0.0.1"
MLX_PORT = 8080
MLX_VLM_PORT = 8081

DEFAULT_ROLE_CAPABILITIES = {
    "reasoning": frozenset({"text", "reasoning", "json", "coach", "long-context"}),
    "code": frozenset({"text", "code", "json"}),
    "classification": frozenset({"text", "classification", "json"}),
    "creative": frozenset({"text", "creative"}),
    "vision": frozenset({"vision", "json"}),
}


class LocalUnavailable(Exception):
    """A local backend could not serve the request (down/missing/over-budget).

    Callers degrade gracefully on this, they never silently fall back to a
    cloud provider.
    """


@dataclass
class BackendSpec:
    """A resolved serving target."""

    model: str
    backend: str  # ollama | mlx | mlx_vlm | remote | cloud
    endpoint: str
    approx_gb: Optional[float] = None
    role: Optional[str] = None
    quant: Optional[str] = None


def infer_backend(model: str) -> str:
    """Heuristic for a model string not found in the registry.

    Ollama tags look like ``name:tag`` with no slash; MLX/HF repos look like
    ``org/repo``. A ``hf.co/<repo>:<quant>`` GGUF pull ref is Ollama's even
    though it contains a slash, so it is special-cased. Everything else with a
    slash is treated as MLX; otherwise Ollama (the back-compat default).
    """
    m = model or ""
    if m.startswith("hf.co/"):
        return "ollama"
    return "mlx" if "/" in m else "ollama"


def default_endpoint(backend: str, config: Any = None) -> str:
    """Default OpenAI-compatible base URL for a backend.

    A ``config.backends`` entry may override any of these, including a
    non-localhost URL for the multi-machine (remote) story.
    """
    endpoints = _config_backends(config)
    if backend in endpoints:
        return endpoints[backend]
    if backend == "mlx_vlm":
        return f"http://{MLX_HOST}:{MLX_VLM_PORT}"
    if backend == "mlx":
        return f"http://{MLX_HOST}:{MLX_PORT}"
    return DEFAULT_OLLAMA_URL


def _config_backends(config: Any) -> dict:
    """The ``backends`` endpoint map from config, or {} if unset. Never raises."""
    if config is None:
        return {}
    backends = _get(config, "backends", {})
    if isinstance(backends, dict):
        return {k: v for k, v in backends.items() if isinstance(v, str)}
    return {}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Attribute-or-key access, so a pydantic Config OR a plain dict both work."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _roles(config: Any) -> dict:
    """The effective role table, layered by precedence (highest wins, per role):

      1. ``config.roles``, an explicit user/test override.
      2. the runtime registry (``registry.yaml``), promoted incumbents.
      3. shipped SUGGESTED defaults (``registry.default_roles()``), a sane
         starting fleet so a fresh install renders instead of blank.

    Layering is per-role: a default only fills a role neither the config nor the
    registry provides. A user's config.roles or a promoted registry entry ALWAYS
    wins for that role, and the defaults are never written back to the registry.
    """
    from ..registry import registry as _registry

    merged: dict = dict(_registry.default_roles())

    loaded = _registry.load().get("roles", {})
    if isinstance(loaded, dict):
        merged.update(loaded)

    roles = _get(config, "roles", {})
    if isinstance(roles, dict):
        merged.update(roles)

    return merged


def role_sources(config: Any = None) -> dict[str, str]:
    """Per-role provenance for the effective role table: ``"config"`` |
    ``"registry"`` | ``"default"``. Mirrors the precedence in :func:`_roles`
    (config wins over registry wins over the shipped defaults) so a reader can
    tell which roles are real vs suggested-but-not-yet-downloaded defaults.
    """
    from ..registry import registry as _registry

    src: dict[str, str] = {r: "default" for r in _registry.default_roles()}
    loaded = _registry.load().get("roles", {})
    if isinstance(loaded, dict):
        for r in loaded:
            src[r] = "registry"
    roles = _get(config, "roles", {})
    if isinstance(roles, dict):
        for r in roles:
            src[r] = "config"
    return src


def resolve(
    role: Optional[str] = None,
    model: Optional[str] = None,
    *,
    config: Any = None,
    default_model: Optional[str] = None,
) -> BackendSpec:
    """Resolve a role or a model name to a :class:`BackendSpec`.

    Precedence: an explicit ``model`` (inheriting role metadata if it matches a
    role entry) wins over ``role``. Unknown models get an inferred backend and
    the default endpoint for it.

    A ``model`` of the form ``role:<name>`` is the broker's wire convention for
    "resolve this role live" (the client sends it, and ``/v1/models`` advertises
    it). It is decoded here to a role lookup so the whole role table (config,
    registry, shipped defaults) applies, rather than being treated as a literal
    model id.
    """
    if model and model.startswith("role:"):
        return resolve(role=model[len("role:"):], config=config, default_model=default_model)

    roles = _roles(config)

    if role and not model:
        spec = roles.get(role)
        model_name = _get(spec, "model") if spec is not None else None
        if not model_name:
            raise LocalUnavailable(f"role {role!r} has no model configured")
        if isinstance(model_name, str) and model_name.startswith("role:"):
            # A role entry whose model is itself a role: pointer is a config error;
            # resolving it as a literal would 404 downstream with no clear cause.
            raise LocalUnavailable(
                f"role {role!r} maps to {model_name!r}, which is not a concrete "
                f"model id (a role must point at a real model, not another role)"
            )
        backend = _get(spec, "backend") or infer_backend(model_name)
        return BackendSpec(
            model=model_name,
            backend=backend,
            endpoint=_get(spec, "endpoint") or default_endpoint(backend, config),
            approx_gb=_get(spec, "approx_gb"),
            role=role,
            quant=_get(spec, "quant"),
        )

    if not model:
        model = default_model
    if not model:
        raise LocalUnavailable("no model or role given and no default configured")

    # If the model matches a role entry, inherit that entry's metadata.
    for rname, spec in roles.items():
        if _get(spec, "model") == model:
            backend = _get(spec, "backend") or infer_backend(model)
            return BackendSpec(
                model=model,
                backend=backend,
                endpoint=_get(spec, "endpoint") or default_endpoint(backend, config),
                approx_gb=_get(spec, "approx_gb"),
                role=rname,
                quant=_get(spec, "quant"),
            )

    backend = infer_backend(model)
    return BackendSpec(
        model=model,
        backend=backend,
        endpoint=default_endpoint(backend, config),
        approx_gb=None,
        role=None,
        quant=None,
    )


def role_candidates(role: str, *, config: Any = None) -> list[BackendSpec]:
    """Ordered compatible serving choices for a role.

    The registry owns this order: incumbent, fallback, then retained rollback.
    Job/model quarantines live one layer above this function; this list only
    removes the role's global known-bad entries and duplicates.
    """

    role_entry = _roles(config).get(role)
    if role_entry is None or not _get(role_entry, "model"):
        raise LocalUnavailable(f"role {role!r} has no model configured")
    quarantined = set(_get(role_entry, "quarantine", []) or [])
    ordered = [
        _get(role_entry, "model"),
        _get(role_entry, "fallback"),
        *(_get(role_entry, "rollback", []) or []),
    ]
    out: list[BackendSpec] = []
    seen: set[str] = set()
    incumbent = ordered[0]
    for model in ordered:
        if not model or model in seen or model in quarantined:
            continue
        seen.add(model)
        if model == incumbent:
            out.append(resolve(role=role, config=config))
            continue
        backend = infer_backend(model)
        out.append(BackendSpec(
            model=model,
            backend=backend,
            endpoint=default_endpoint(backend, config),
            role=role,
        ))
    return out


def role_capabilities(role: str, *, config: Any = None) -> frozenset[str]:
    """Capabilities declared by role policy, never inferred from a caller request."""
    entry = _roles(config).get(role) or {}
    declared = _get(entry, "capabilities")
    if declared is None:
        return DEFAULT_ROLE_CAPABILITIES.get(role, frozenset())
    if not isinstance(declared, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(str(item) for item in declared if item)


def port_of(spec: BackendSpec, default: int = MLX_PORT) -> int:
    """Extract the TCP port from a spec's endpoint (for MLX server targeting)."""
    ep = spec.endpoint or default_endpoint("mlx")
    try:
        return int(ep.rsplit(":", 1)[1].split("/")[0])
    except Exception:  # noqa: BLE001
        return default
