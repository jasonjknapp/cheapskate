# SPDX-License-Identifier: Apache-2.0
"""Python client for the broker's OpenAI-compatible HTTP API.

Two entry points:

  * :func:`complete` — one unstructured text completion.
  * :func:`generate_json` — a structured JSON completion (optionally validated
    against a JSON schema or a pydantic model), with a repair-nudge retry loop.

Both route through the broker so the single-admission-queue and one-large-model
safety hold. Graceful degradation is the contract: on ANY broker problem they
raise :class:`CheapskateUnavailable`. They NEVER silently fall back to a cloud
provider — the caller decides how to degrade.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx

from .broker.app import DEFAULT_HOST, DEFAULT_PORT
from .broker.gates import DEFAULT_CLASS, keys_path

DEFAULT_TIMEOUT = 300  # seconds; a cold large-model first-load can exceed 120s


class CheapskateUnavailable(Exception):
    """The broker could not produce a usable response (down/missing/invalid).

    Callers degrade gracefully — they never silently fall back to cloud.
    """


def _broker_base(config: Any = None) -> str:
    """The broker base URL. Overridable via the CHEAPSKATE_BROKER_URL env var,
    then the config's ``broker`` block, then localhost defaults."""
    env = os.environ.get("CHEAPSKATE_BROKER_URL")
    if env:
        return env.rstrip("/")
    host, port = DEFAULT_HOST, DEFAULT_PORT
    broker = _get(config, "broker", {}) or {}
    host = _get(broker, "host", host) or host
    port = _get(broker, "port", port) or port
    return f"http://{host}:{port}"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _api_key(config: Any = None, *, cls: str = DEFAULT_CLASS) -> Optional[str]:
    """First registered key of QoS class ``cls`` from the broker keys file, or the
    CHEAPSKATE_API_KEY env var if set. Secrets come from the environment or the
    0600 key file only — never from config.yaml."""
    env = os.environ.get("CHEAPSKATE_API_KEY")
    if env:
        return env
    try:
        keys = json.loads(Path(keys_path(config)).read_text())
    except Exception:  # noqa: BLE001
        return None
    for k, v in keys.items():
        if v.get("class") == cls:
            return k
    # Fall back to any registered key.
    for k in keys:
        return k
    return None


def _post_chat(
    messages: list,
    *,
    model: Optional[str],
    role: Optional[str],
    temperature: float,
    timeout: float,
    response_json: bool,
    config: Any,
    api: Optional[Any] = None,
) -> dict:
    """POST one chat completion to the broker. Returns the parsed JSON body.

    Raises :class:`CheapskateUnavailable` on any transport/broker failure.
    ``api`` is an injectable HTTP client (must expose ``.post(url, json=, headers=,
    timeout=)`` returning an httpx-like response); defaults to a fresh httpx client.
    """
    key = _api_key(config)
    if not key:
        raise CheapskateUnavailable(
            "no broker API key available (set CHEAPSKATE_API_KEY or register a key)"
        )
    model_field = f"role:{role}" if role else (model or "")
    payload: dict[str, Any] = {
        "model": model_field, "messages": messages,
        "stream": False, "temperature": temperature,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}
    url = f"{_broker_base(config)}/v1/chat/completions"
    # X-Cheapskate-Internal marks a call that originated from cheapskate's own
    # router (which already emits the costable generation telemetry). The broker
    # uses it to avoid double-counting: it records only an ops event for these,
    # and reserves its own cost event for genuinely external OpenAI-compat traffic.
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "X-Cheapskate-Internal": "1",
    }

    client = api or httpx.Client(timeout=timeout)
    close = api is None
    try:
        resp = client.post(url, json=payload, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — any broker failure degrades gracefully
        raise CheapskateUnavailable(f"broker unreachable: {e}") from e
    finally:
        if close:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    if resp.status_code >= 400:
        detail = _text(resp)[:300]
        raise CheapskateUnavailable(f"broker HTTP {resp.status_code}: {detail}")
    try:
        return resp.json()
    except Exception as e:  # noqa: BLE001
        raise CheapskateUnavailable(f"broker returned non-JSON: {e}") from e


def _text(resp: Any) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001
        return ""


def _content(body: dict) -> str:
    try:
        text = body["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        raise CheapskateUnavailable(f"unexpected broker response shape: {e}") from e
    if not text:
        raise CheapskateUnavailable("broker returned empty content")
    return text


def complete(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    role: Optional[str] = None,
    temperature: float = 0.2,
    timeout: float = DEFAULT_TIMEOUT,
    config: Any = None,
    api: Optional[Any] = None,
) -> dict:
    """Run one unstructured completion through the broker.

    Specify EITHER ``role`` (resolved live from the registry) OR ``model`` (a
    concrete tag). Returns ``{text, model, latency_s, eval_count,
    prompt_eval_count}``. Raises :class:`CheapskateUnavailable` on failure.
    """
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    start = time.monotonic()
    body = _post_chat(
        messages, model=model, role=role, temperature=temperature,
        timeout=timeout, response_json=False, config=config, api=api,
    )
    text = _content(body)
    usage = body.get("usage") or {}
    return {
        "text": text,
        "model": body.get("model", model),
        "latency_s": round(time.monotonic() - start, 2),
        "eval_count": usage.get("completion_tokens"),
        "prompt_eval_count": usage.get("prompt_tokens"),
    }


def _schema_format(schema: Any):
    """Return a pydantic validator for a pydantic model class, else None.

    A raw JSON-schema dict or None yields no validator (the result is parsed with
    ``json.loads`` and returned as-is).
    """
    if schema is None:
        return None
    if hasattr(schema, "model_validate_json"):  # pydantic v2 model class
        return schema
    if isinstance(schema, dict):
        return None
    raise TypeError(f"schema must be a pydantic model or dict, got {type(schema)}")


def generate_json(
    prompt: str,
    *,
    schema: Any = None,
    system: Optional[str] = None,
    model: Optional[str] = None,
    role: Optional[str] = None,
    temperature: float = 0.1,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    config: Any = None,
    api: Optional[Any] = None,
) -> Union[dict, list]:
    """Run one structured-JSON completion through the broker. Returns the parsed
    object.

    ``schema`` may be a pydantic model class (validated + retried) or a raw
    JSON-schema dict. On an invalid response the model is nudged to repair, up to
    ``retries`` times. Raises :class:`CheapskateUnavailable` on a hard failure or
    if still invalid after the retries. NEVER falls back to cloud.
    """
    validator = _schema_format(schema)
    sys_msg = ((system or "") + "\nReturn ONLY valid JSON matching the required schema.").strip()
    messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ]
    last_err: Optional[Exception] = None
    for _ in range(retries + 1):
        body = _post_chat(
            messages, model=model, role=role, temperature=temperature,
            timeout=timeout, response_json=True, config=config, api=api,
        )
        text = _content(body)
        try:
            if validator is not None:
                return validator.model_validate_json(text).model_dump()
            return json.loads(text)
        except CheapskateUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — bad JSON / schema mismatch → retry
            last_err = e
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": "That was not valid per the required schema. "
                           "Return ONLY valid JSON matching the schema.",
            })
    raise CheapskateUnavailable(
        f"broker did not return schema-valid JSON after {retries + 1} tries: {last_err}"
    )
