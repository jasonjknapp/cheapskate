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
from urllib.parse import urlparse

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
    privacy: str,
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
        "X-Model-Privacy": privacy,
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
    privacy: str = "never_cloud",
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
    candidates = [(model, role)]
    if role and model is None:
        from .backends.resolve import role_candidates

        candidates.extend(
            (candidate.model, None)
            for candidate in role_candidates(role, config=config)[1:]
        )

    body = None
    text = None
    last_error = None
    for candidate_model, candidate_role in candidates:
        if privacy == "never_cloud" and not _never_cloud_route_is_local(
            model=candidate_model, role=candidate_role, config=config,
        ):
            last_error = CheapskateUnavailable(
                "never_cloud requires a verified local backend with loopback "
                "broker and serving endpoints"
            )
            continue
        try:
            candidate_body = _post_chat(
                messages, model=candidate_model, role=candidate_role,
                temperature=temperature, timeout=timeout,
                response_json=False, privacy=privacy, config=config, api=api,
            )
            candidate_text = _content(candidate_body)
            body, text = candidate_body, candidate_text
            break
        except CheapskateUnavailable as exc:
            last_error = exc
    if body is None or text is None:
        if not role or model is not None:
            raise last_error or CheapskateUnavailable("completion failed")
        raise CheapskateUnavailable(
            f"role {role!r} and its compatible fallbacks were unavailable: {last_error}"
        ) from last_error
    usage = body.get("usage") or {}
    return {
        "text": text,
        # ``model`` stays substituted for display/back-compat; ``served_model`` is
        # the RAW backend identity (None when the backend omitted it) so callers
        # can verify attribution and fail closed on missing provenance rather than
        # trusting the requested name we substituted in.
        "model": body.get("model", model),
        "served_model": body.get("model"),
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


def _validate_raw_schema(value: Any, schema: dict, path: str = "$") -> Any:
    """Validate the dependency-free JSON-schema subset used by clients."""
    expected = schema.get("type")
    type_map = {
        "object": dict, "array": list, "string": str,
        "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
    }
    if expected in type_map:
        valid = isinstance(value, type_map[expected])
        if expected in {"number", "integer"} and isinstance(value, bool):
            valid = False
        if not valid:
            raise ValueError(f"{path} must be {expected}, got {type(value).__name__}")
    if isinstance(value, dict):
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"{path} missing required keys: {', '.join(missing)}")
        properties = schema.get("properties") or {}
        for key, child in value.items():
            if key in properties:
                _validate_raw_schema(child, properties[key], f"{path}.{key}")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(f"{path} has unexpected keys: {', '.join(extras)}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} exceeds maxItems={schema['maxItems']}")
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} is below minItems={schema['minItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_raw_schema(child, item_schema, f"{path}[{index}]")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not one of the permitted values")
    return value


def _candidate_installed(spec: Any) -> bool:
    """Probe actual local installation state without downloading anything."""
    if spec.backend == "ollama":
        from .backends.ollama import ollama_model_present
        return ollama_model_present(spec.model)
    if spec.backend in {"mlx", "mlx_vlm"} and "/" in spec.model:
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        dirname = "models--" + spec.model.replace("/", "--")
        return (hf_home / "hub" / dirname).is_dir()
    return False


def _endpoint_is_local(endpoint: str | None) -> bool:
    """True only for explicit loopback HTTP endpoints; unknown fails closed."""
    try:
        host = urlparse(str(endpoint)).hostname
    except ValueError:
        return False
    return host in {"localhost", "127.0.0.1", "::1"}


_LOCAL_SERVING_BACKENDS = frozenset({"ollama", "mlx", "mlx_vlm", "lmstudio"})


def _serving_spec_is_local(spec: Any) -> bool:
    return (
        spec.backend in _LOCAL_SERVING_BACKENDS
        and _endpoint_is_local(spec.endpoint)
    )


def _never_cloud_route_is_local(*, model: str | None, role: str | None, config: Any) -> bool:
    """Prove the broker and a known-local serving backend stay on loopback."""
    if not _endpoint_is_local(_broker_base(config)):
        return False
    try:
        from .backends.resolve import resolve

        spec = resolve(role=role if model is None else None, model=model, config=config)
    except Exception:  # noqa: BLE001 - unknown provenance fails closed
        return False
    return _serving_spec_is_local(spec)


def _never_cloud_role_has_local_candidate(role: str, config: Any) -> bool:
    """True only when the broker is loopback and the role offers at least one
    known-local serving candidate. A role whose incumbent and every fallback
    resolve to a nonlocal backend has no verified local route, so never_cloud
    must refuse it up front rather than surface a generic no-model error."""
    if not _endpoint_is_local(_broker_base(config)):
        return False
    try:
        from .backends.resolve import role_candidates

        return any(
            _serving_spec_is_local(spec)
            for spec in role_candidates(role, config=config)
        )
    except Exception:  # noqa: BLE001 - unknown provenance fails closed
        return False


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
    job_id: Optional[str] = None,
    required_capabilities: Optional[set[str]] = None,
    quality_floor: float = 1.0,
    privacy: str = "never_cloud",
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
    if privacy == "never_cloud":
        # A role request may have a nonlocal incumbent but a valid installed
        # local fallback. Prove the broker hop plus at least one local role
        # candidate here; the self-healing fleet and invoke-time guard prove
        # each concrete candidate below.
        if model is not None:
            route_is_valid = _never_cloud_route_is_local(
                model=model, role=None, config=config,
            )
        elif role is not None:
            route_is_valid = _never_cloud_role_has_local_candidate(role, config)
        else:
            route_is_valid = False
        if not route_is_valid:
            raise CheapskateUnavailable(
                "never_cloud requires a verified local backend with loopback "
                "broker and serving endpoints"
            )
    validator = _schema_format(schema)
    sys_msg = ((system or "") + "\nReturn ONLY valid JSON matching the required schema.").strip()
    base_messages = [
        {"role": "system", "content": sys_msg},
        {"role": "user", "content": prompt},
    ]
    def parse_response(text: str) -> Union[dict, list]:
        try:
            if validator is not None:
                return validator.model_validate_json(text).model_dump()
            parsed = json.loads(text)
            if isinstance(schema, dict):
                _validate_raw_schema(parsed, schema)
            elif not isinstance(parsed, dict):
                raise ValueError(f"$ must be object, got {type(parsed).__name__}")
            return parsed
        except Exception as exc:
            raise ValueError(f"schema validation failed: {exc}") from exc

    if role and model is None:
        from . import paths
        from .backends.resolve import role_candidates, role_capabilities
        from .contracts import JobContract
        from .self_healing import (
            Candidate,
            CompatibilityStore,
            NoCompatibleModel,
            SelfHealingEngine,
        )

        required = frozenset(required_capabilities or {"json"})
        declared = role_capabilities(role, config=config)
        contract = JobContract(
            job_id=job_id or f"client.generate_json:{role}",
            role=role,
            output_mode="json",
            required_capabilities=required,
            repair_attempts=retries,
            quality_floor=quality_floor,
            privacy=privacy,
        )
        installed = [
            Candidate(
                spec.model,
                spec.backend,
                capabilities=declared,
                installed=_candidate_installed(spec),
                local=_serving_spec_is_local(spec),
            )
            for spec in role_candidates(role, config=config)
        ]
        engine = SelfHealingEngine(compatibility=CompatibilityStore(
            paths.state_dir() / "model-job-compatibility.json"
        ))

        def invoke(candidate: Candidate, feedback: str | None) -> Union[dict, list]:
            if privacy == "never_cloud" and not _never_cloud_route_is_local(
                model=candidate.model, role=None, config=config,
            ):
                raise CheapskateUnavailable(
                    "never_cloud candidate route changed or is not verified local"
                )
            messages = list(base_messages)
            if feedback:
                messages.append({
                    "role": "user",
                    "content": "The prior response failed validation: "
                    f"{feedback}. Return only corrected schema-valid JSON.",
                })
            body = _post_chat(
                messages,
                model=candidate.model,
                role=None,
                temperature=temperature,
                timeout=timeout,
                response_json=True,
                privacy=privacy,
                config=config,
                api=api,
            )
            # Fail closed on served-model identity: attributing candidate A's
            # compatibility/quarantine to a response that a hidden fallback served
            # as B corrupts the self-healing state. A backend that omits the field
            # (served None) is also rejected — provenance is required, not assumed.
            served = body.get("model") if isinstance(body, dict) else None
            if served != candidate.model:
                raise CheapskateUnavailable(
                    f"requested {candidate.model!r} but broker served {served!r}"
                )
            return parse_response(_content(body))

        try:
            return engine.run(
                contract,
                installed,
                invoke=invoke,
                validate=lambda _value: (True, "", 1.0),
            ).output
        except NoCompatibleModel as exc:
            raise CheapskateUnavailable(str(exc)) from exc

    last_err: Optional[Exception] = None
    for candidate in [model]:
        messages = list(base_messages)
        for _ in range(retries + 1):
            try:
                body = _post_chat(
                    messages, model=candidate, role=None if candidate else role,
                    temperature=temperature, timeout=timeout, response_json=True,
                    privacy=privacy, config=config, api=api,
                )
                text = _content(body)
                return parse_response(text)
            except CheapskateUnavailable as exc:
                last_err = exc
            except Exception as exc:  # noqa: BLE001 — bad JSON/schema → repair
                last_err = exc
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": "That was not valid per the required schema. "
                               "Return ONLY valid JSON matching the schema.",
                })
    raise CheapskateUnavailable(
        f"no role-compatible model returned schema-valid JSON: {last_err}"
    )
