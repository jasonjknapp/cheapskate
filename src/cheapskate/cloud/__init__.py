# SPDX-License-Identifier: Apache-2.0
"""Thin cloud tier: adapters that dispatch a completion to a cloud provider.

Cheapskate never rebuilds a multi-provider gateway. This package is deliberately
minimal — two adapter kinds cover the field:

  * ``openai-compat`` — any OpenAI-compatible HTTP API (OpenAI, OpenRouter, a
    Gemini OpenAI-compat endpoint) selected by ``base_url``.
  * ``anthropic`` — Claude via the Anthropic SDK.

Both are LAZY: the ``openai`` / ``anthropic`` SDKs are optional extras, imported
only when a provider of that kind actually dispatches, so a base install runs
without either. Secrets come from the environment ONLY (Hard rule 3) — the
provider config names the env var; the key never touches config.yaml or the repo.
"""

from __future__ import annotations

from .adapters import (
    CloudError,
    CloudResult,
    dispatch_role,
    provider_for_role,
)

__all__ = [
    "CloudError",
    "CloudResult",
    "dispatch_role",
    "provider_for_role",
]
