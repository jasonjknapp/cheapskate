# SPDX-License-Identifier: Apache-2.0
"""Cloud adapters: dispatch one completion to a configured cloud provider.

Two kinds, both thin:

  * ``openai-compat`` — the ``openai`` SDK pointed at any OpenAI-compatible
    ``base_url`` (OpenAI, OpenRouter, a Gemini OpenAI-compat endpoint).
  * ``anthropic`` — the ``anthropic`` SDK (Claude).

Design rules:
  * LAZY imports — the SDKs are optional extras; a base install imports this
    module fine and only fails (with an actionable message naming the missing
    extra) when a provider of that kind actually dispatches.
  * Secrets from the ENVIRONMENT only (Hard rule 3). The provider config names
    the env var; a disabled provider or a missing key is a clear, catchable
    :class:`CloudError` — never a silent skip.
  * The SDK client is INJECTABLE (``client=``) so tests exercise the adapter
    with a fake and never touch the network or a real key.

Every successful dispatch returns a :class:`CloudResult` with the same shape
regardless of provider kind: ``{text, model, tokens_in, tokens_out, latency_s}``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from ..config import Config, ProviderConfig

# The extra a caller must install for each provider kind, named in the error so
# the fix is obvious ("pip install cheapskate[openai]").
_EXTRA_FOR_KIND = {
    "openai-compat": "openai",
    "anthropic": "anthropic",
}


class CloudError(Exception):
    """A cloud dispatch could not proceed or complete.

    Raised for a missing extra, an unset/empty API-key env var, an unmapped
    role, an unknown provider kind, or a provider/SDK failure. It is a HARD
    error by design — the router never silently swallows it into a local
    fallback (that direction is a policy decision, made upstream)."""


@dataclass(frozen=True)
class CloudResult:
    """The uniform result of a cloud completion, across provider kinds."""

    text: str
    model: str
    tokens_in: int | None
    tokens_out: int | None
    latency_s: float


# ── provider selection ───────────────────────────────────────────────────────


def enabled_providers(config: Config) -> dict[str, ProviderConfig]:
    """Every provider marked ``enabled`` in config. Empty ⇒ the cloud tier is
    off (the shipped default)."""
    return {name: p for name, p in config.providers.items() if p.enabled}


def provider_for_role(config: Config, role: str) -> tuple[str, ProviderConfig, str]:
    """Pick the enabled provider that can serve ``role`` and its concrete model.

    Returns ``(provider_name, provider, model_id)``. Deterministic: providers
    are considered in sorted-name order, first hit wins. Raises
    :class:`CloudError` — with an actionable message — when no enabled provider
    maps the role (this is the "cloud route with no enabled provider" hard
    error)."""
    enabled = enabled_providers(config)
    if not enabled:
        raise CloudError(
            "cloud route requested but no provider is enabled; enable one under "
            "config 'providers' and set its api_key_env in the environment"
        )
    for name in sorted(enabled):
        provider = enabled[name]
        model_id = provider.model_map.get(role)
        if model_id:
            return (name, provider, model_id)
    have = ", ".join(sorted(enabled)) or "(none)"
    raise CloudError(
        f"no enabled cloud provider maps role {role!r}; enabled providers: {have}. "
        f"Add a '{role}' entry to a provider's model_map"
    )


# ── secrets ──────────────────────────────────────────────────────────────────


def _api_key(provider: ProviderConfig) -> str:
    """Read the provider's API key from its configured env var. A missing
    ``api_key_env`` or an unset/empty value is a hard :class:`CloudError` —
    never a silent fall-through."""
    env_name = provider.api_key_env
    if not env_name:
        raise CloudError(
            "provider has no api_key_env configured; name the env var holding "
            "its secret (the key itself never lives in config)"
        )
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise CloudError(
            f"cloud API key env var {env_name!r} is unset or empty; "
            f"export it before enabling this provider"
        )
    return key


# ── dispatch ─────────────────────────────────────────────────────────────────


def dispatch_role(
    config: Config,
    role: str,
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    client: Any = None,
) -> CloudResult:
    """Dispatch ``prompt`` for ``role`` to the first enabled provider that maps
    it. Resolves the provider, then delegates to the kind-specific adapter.

    ``client`` (an injected SDK client) is passed straight through so tests run
    without the network or a real key. Raises :class:`CloudError` on any failure.
    """
    _name, provider, model_id = provider_for_role(config, role)
    return dispatch_provider(
        provider,
        model_id,
        prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        client=client,
    )


def dispatch_provider(
    provider: ProviderConfig,
    model_id: str,
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    client: Any = None,
) -> CloudResult:
    """Dispatch to a specific provider + model. The kind selects the adapter."""
    kind = provider.kind
    if kind == "openai-compat":
        return _dispatch_openai_compat(
            provider, model_id, prompt, system, temperature, max_tokens, client
        )
    if kind == "anthropic":
        return _dispatch_anthropic(
            provider, model_id, prompt, system, temperature, max_tokens, client
        )
    raise CloudError(
        f"unknown provider kind {kind!r}; supported: openai-compat, anthropic"
    )


def _require_sdk(kind: str) -> Any:
    """Lazy-import the SDK for a provider kind, or raise a CloudError naming the
    missing optional extra."""
    extra = _EXTRA_FOR_KIND.get(kind, kind)
    try:
        if kind == "openai-compat":
            import openai  # noqa: PLC0415 — lazy by design (optional extra)

            return openai
        if kind == "anthropic":
            import anthropic  # noqa: PLC0415 — lazy by design (optional extra)

            return anthropic
    except ImportError as exc:
        raise CloudError(
            f"the {kind!r} provider needs the '{extra}' extra; "
            f"install it with: pip install 'cheapskate[{extra}]'"
        ) from exc
    raise CloudError(f"unknown provider kind {kind!r}")


def _dispatch_openai_compat(
    provider: ProviderConfig,
    model_id: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    client: Any,
) -> CloudResult:
    if client is None:
        openai = _require_sdk("openai-compat")
        key = _api_key(provider)
        kwargs: dict[str, Any] = {"api_key": key}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url
        client = openai.OpenAI(**kwargs)

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    started = time.monotonic()
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001 — any SDK/transport failure is a hard cloud error
        raise CloudError(f"openai-compat dispatch failed: {type(exc).__name__}: {exc}") from exc
    latency = round(time.monotonic() - started, 3)

    text = _openai_text(resp)
    usage = _attr(resp, "usage")
    return CloudResult(
        text=text,
        model=_attr(resp, "model", model_id) or model_id,
        tokens_in=_int_or_none(_attr(usage, "prompt_tokens")),
        tokens_out=_int_or_none(_attr(usage, "completion_tokens")),
        latency_s=latency,
    )


def _dispatch_anthropic(
    provider: ProviderConfig,
    model_id: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    client: Any,
) -> CloudResult:
    if client is None:
        anthropic = _require_sdk("anthropic")
        key = _api_key(provider)
        kwargs: dict[str, Any] = {"api_key": key}
        if provider.base_url:
            kwargs["base_url"] = provider.base_url
        client = anthropic.Anthropic(**kwargs)

    started = time.monotonic()
    try:
        create_kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            create_kwargs["system"] = system
        resp = client.messages.create(**create_kwargs)
    except CloudError:
        raise
    except Exception as exc:  # noqa: BLE001 — any SDK/transport failure is a hard cloud error
        raise CloudError(f"anthropic dispatch failed: {type(exc).__name__}: {exc}") from exc
    latency = round(time.monotonic() - started, 3)

    text = _anthropic_text(resp)
    usage = _attr(resp, "usage")
    return CloudResult(
        text=text,
        model=_attr(resp, "model", model_id) or model_id,
        tokens_in=_int_or_none(_attr(usage, "input_tokens")),
        tokens_out=_int_or_none(_attr(usage, "output_tokens")),
        latency_s=latency,
    )


# ── response shape helpers (tolerant of SDK objects and plain dicts) ─────────


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _openai_text(resp: Any) -> str:
    """Pull the assistant text out of an OpenAI chat-completions response,
    tolerant of both SDK objects and plain dicts (for injected fakes)."""
    choices = _attr(resp, "choices") or []
    if not choices:
        raise CloudError("openai-compat response had no choices")
    message = _attr(choices[0], "message")
    text = _attr(message, "content")
    if not text:
        raise CloudError("openai-compat response had empty content")
    return text


def _anthropic_text(resp: Any) -> str:
    """Concatenate the text blocks of an Anthropic messages response."""
    blocks = _attr(resp, "content") or []
    parts: list[str] = []
    for block in blocks:
        if _attr(block, "type", "text") == "text":
            piece = _attr(block, "text")
            if piece:
                parts.append(piece)
    text = "".join(parts)
    if not text:
        raise CloudError("anthropic response had empty content")
    return text
