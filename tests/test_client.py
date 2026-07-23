# SPDX-License-Identifier: Apache-2.0
"""Pins the public client API (complete / generate_json) via an injected HTTP
client. No network. Graceful degrade surfaces as CheapskateUnavailable."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cheapskate import client
from cheapskate.broker.gates import genkey


@pytest.fixture
def registered_key(tmp_path, monkeypatch):
    """Register an interactive key in the isolated state dir and return it."""
    from cheapskate.broker import gates

    path = gates.keys_path()
    key = genkey("tester", "interactive", path=path)
    return key


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class FakeClient:
    """Injected via api=. Records the request and returns a queued response."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _chat_body(content, model="test-model"):
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": {"completion_tokens": 5, "prompt_tokens": 3},
    }


# ── complete ────────────────────────────────────────────────────────────────


def test_complete_returns_text_and_metadata(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body("hello world"))])
    out = client.complete("hi", role="reasoning", api=api)
    assert out["text"] == "hello world"
    assert out["model"] == "test-model"
    assert out["eval_count"] == 5
    # Routed to the broker chat endpoint, with the concrete resolved incumbent
    # (H3: client-side quarantine-aware resolution, not the role: wire seed) + key.
    req = api.requests[0]
    assert req["url"].endswith("/v1/chat/completions")
    assert req["json"]["model"] == "gpt-oss:120b"  # default "reasoning" incumbent
    assert req["headers"]["Authorization"] == f"Bearer {registered_key}"
    # D8: the internal marker so the broker does not double-count this call in econ.
    assert req["headers"]["X-Cheapskate-Internal"] == "1"
    assert req["headers"]["X-Model-Privacy"] == "never_cloud"


def test_complete_role_fails_over_to_registered_fallback(registered_key):
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent", "backend": "mlx", "fallback": "fallback:latest",
    }}}
    api = FakeClient([
        FakeResponse(503, {"error": "incumbent unavailable"}),
        FakeResponse(200, _chat_body("recovered", model="fallback:latest")),
    ])
    out = client.complete("hi", role="reasoning", config=cfg, api=api)
    assert out["text"] == "recovered"
    assert out["model"] == "fallback:latest"
    assert [req["json"]["model"] for req in api.requests] == [
        "org/incumbent", "fallback:latest",
    ]


@pytest.mark.parametrize("bad_body", [
    _chat_body(""),
    {"model": "org/incumbent", "choices": []},
])
def test_complete_role_fails_over_after_malformed_success(registered_key, bad_body):
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent", "backend": "mlx", "fallback": "fallback:latest",
    }}}
    api = FakeClient([
        FakeResponse(200, bad_body),
        FakeResponse(200, _chat_body("recovered", model="fallback:latest")),
    ])
    out = client.complete("hi", role="reasoning", config=cfg, api=api)
    assert out["text"] == "recovered"
    assert [request["json"]["model"] for request in api.requests] == [
        "org/incumbent", "fallback:latest",
    ]


def test_complete_role_quarantined_incumbent_serves_fallback(registered_key):
    """H3: when the incumbent is globally quarantined, complete() must serve the
    eligible fallback and NEVER re-serve the quarantined incumbent (the old
    (None, role) seed resolved past quarantine straight back to the incumbent)."""
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent", "backend": "mlx",
        "fallback": "fallback:latest",
        "quarantine": ["org/incumbent"],
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body("from fallback", model="fallback:latest")),
    ])
    out = client.complete("hi", role="reasoning", config=cfg, api=api)
    assert out["text"] == "from fallback"
    # Exactly one request, and it is the fallback — the incumbent is never sent.
    assert [req["json"]["model"] for req in api.requests] == ["fallback:latest"]


def test_complete_role_no_quarantine_serves_incumbent(registered_key):
    """H3 regression guard: with no quarantine the incumbent still serves first."""
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent", "backend": "mlx",
        "fallback": "fallback:latest",
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body("from incumbent", model="org/incumbent")),
    ])
    out = client.complete("hi", role="reasoning", config=cfg, api=api)
    assert out["text"] == "from incumbent"
    assert [req["json"]["model"] for req in api.requests] == ["org/incumbent"]


def test_complete_role_fully_quarantined_raises_naming_role(registered_key):
    """H3: a role whose every candidate is quarantined surfaces a clear
    CheapskateUnavailable that names the role — never a bare None dispatch."""
    cfg = {"roles": {"reasoning": {
        "model": "org/incumbent", "backend": "mlx",
        "fallback": "fallback:latest",
        "quarantine": ["org/incumbent", "fallback:latest"],
    }}}
    api = FakeClient([])
    with pytest.raises(client.CheapskateUnavailable, match="reasoning"):
        client.complete("hi", role="reasoning", config=cfg, api=api)
    assert api.requests == []


def test_complete_passes_system_prompt(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body("ok"))])
    client.complete("q", system="be terse", model="m:tag", api=api)
    msgs = api.requests[0]["json"]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1]["content"] == "q"


def test_complete_missing_key_degrades(monkeypatch):
    # No key registered, no env var → graceful degrade, not a crash.
    monkeypatch.delenv("CHEAPSKATE_API_KEY", raising=False)
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="m", api=FakeClient([]))


def test_complete_http_error_degrades(registered_key):
    api = FakeClient([FakeResponse(503, {"error": "over budget"})])
    with pytest.raises(client.CheapskateUnavailable) as e:
        client.complete("hi", model="m", api=api)
    assert "503" in str(e.value)


def test_complete_transport_error_degrades(registered_key):
    class Boom:
        def post(self, *a, **k):
            raise ConnectionError("refused")

    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="m", api=Boom())


def test_complete_empty_content_degrades(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body(""))])
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="m", api=api)


def test_complete_never_falls_back_to_cloud(registered_key):
    # A broker failure raises; it does not silently return a cloud answer.
    api = FakeClient([FakeResponse(502, {"error": "backend blew up"})])
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="m", api=api)
    assert len(api.requests) == 1  # exactly one attempt, no second (cloud) path


@pytest.mark.parametrize(
    ("config", "routing"),
    [
        (None, {"model": "local:latest"}),
        ({"roles": {"reasoning": {
            "model": "remote-model", "backend": "ollama",
            "endpoint": "https://models.example.com/v1",
        }}}, {"role": "reasoning"}),
    ],
)
def test_complete_never_cloud_rejects_remote_route_before_http(
    registered_key, monkeypatch, config, routing
):
    if config is None:
        monkeypatch.setenv("CHEAPSKATE_BROKER_URL", "https://remote.example.com")
    api = FakeClient([FakeResponse(200, _chat_body("private"))])

    with pytest.raises(client.CheapskateUnavailable, match="verified local backend"):
        client.complete("private", config=config, api=api, **routing)
    assert api.requests == []


def test_api_key_from_env_wins(monkeypatch):
    monkeypatch.setenv("CHEAPSKATE_API_KEY", "sk-env-override")
    api = FakeClient([FakeResponse(200, _chat_body("ok"))])
    client.complete("hi", model="m", api=api)
    assert api.requests[0]["headers"]["Authorization"] == "Bearer sk-env-override"


# ── generate_json ───────────────────────────────────────────────────────────


def test_generate_json_parses_object(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body('{"fruit": "apple"}', model="m"))])
    out = client.generate_json("list a fruit", model="m", api=api)
    assert out == {"fruit": "apple"}
    # Structured requests set response_format json_object.
    assert api.requests[0]["json"]["response_format"] == {"type": "json_object"}


def test_generate_json_repairs_then_succeeds(registered_key):
    api = FakeClient([
        FakeResponse(200, _chat_body("not json at all", model="m")),
        FakeResponse(200, _chat_body('{"ok": true}', model="m")),
    ])
    out = client.generate_json("q", model="m", api=api, retries=2)
    assert out == {"ok": True}
    assert len(api.requests) == 2  # one repair round
    # The repair nudge was appended to the conversation.
    second_msgs = api.requests[1]["json"]["messages"]
    assert any("valid JSON" in m["content"] for m in second_msgs if m["role"] == "user")


def test_generate_json_exhausts_retries_and_degrades(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body("garbage", model="m")) for _ in range(3)])
    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json("q", model="m", api=api, retries=2)
    assert len(api.requests) == 3  # retries + 1


def test_generate_json_validates_pydantic_schema(registered_key):
    pydantic = pytest.importorskip("pydantic")

    class Fruit(pydantic.BaseModel):
        name: str
        qty: int

    api = FakeClient([FakeResponse(200, _chat_body('{"name": "pear", "qty": 3}', model="m"))])
    out = client.generate_json("q", schema=Fruit, model="m", api=api)
    assert out == {"name": "pear", "qty": 3}


def test_generate_json_repairs_valid_json_with_wrong_schema_root(registered_key):
    api = FakeClient([
        FakeResponse(200, _chat_body('[{"items": []}]', model="m")),
        FakeResponse(200, _chat_body('{"items": []}', model="m")),
    ])
    schema = {"type": "object", "required": ["items"],
              "properties": {"items": {"type": "array"}}}
    out = client.generate_json("q", schema=schema, model="m", api=api, retries=1)
    assert out == {"items": []}
    assert len(api.requests) == 2


def test_generate_json_explicit_model_accepts_matching_served(registered_key):
    """H4: the explicit-model path serves normally when the broker attributes the
    response to the requested model."""
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}', model="m"))])
    out = client.generate_json("q", model="m", api=api)
    assert out == {"ok": True}


def test_generate_json_explicit_model_rejects_served_mismatch(registered_key):
    """H4: a backend-side fallback that serves a DIFFERENT model than requested is
    a provenance failure — CheapskateUnavailable, no repair nudge, exactly one call."""
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}', model="impostor"))])
    with pytest.raises(client.CheapskateUnavailable, match="broker served"):
        client.generate_json("q", model="m", api=api, retries=0)
    assert len(api.requests) == 1  # provenance failure surfaces; no schema-repair nudge


def test_generate_json_explicit_model_rejects_missing_served(registered_key):
    """H4: a body that omits ``model`` fails closed — provenance is required."""
    body = {"choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"completion_tokens": 5, "prompt_tokens": 3}}
    api = FakeClient([FakeResponse(200, body)])
    with pytest.raises(client.CheapskateUnavailable, match="broker served"):
        client.generate_json("q", model="m", api=api, retries=0)
    assert len(api.requests) == 1


def test_generate_json_transport_error_degrades(registered_key):
    class Boom:
        def post(self, *a, **k):
            raise TimeoutError("slow")

    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json("q", model="m", api=Boom())


def test_generate_json_repairs_then_switches_role_fallback(registered_key, monkeypatch):
    cfg = {"roles": {"classification": {
        "model": "org/incumbent",
        "backend": "mlx",
        "fallback": "fallback:latest",
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body("[]")),
        FakeResponse(200, _chat_body("[]")),
        FakeResponse(200, _chat_body('{"themes": []}', model="fallback:latest")),
    ])

    class Digest:
        @classmethod
        def model_validate_json(cls, text):
            value = __import__("json").loads(text)
            if not isinstance(value, dict):
                raise ValueError("root must be an object")
            return SimpleNamespace(model_dump=lambda: value)

    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    out = client.generate_json(
        "q", schema=Digest, role="classification", config=cfg, api=api, retries=1
    )
    assert out == {"themes": []}
    assert [req["json"]["model"] for req in api.requests] == [
        "org/incumbent", "org/incumbent", "fallback:latest",
    ]


def test_generate_json_skips_remote_incumbent_for_local_fallback(
    registered_key, monkeypatch
):
    cfg = {"roles": {"classification": {
        "model": "remote-incumbent", "backend": "remote",
        "endpoint": "https://models.example.com/v1",
        "fallback": "local:latest",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body('{"ok": true}', model="local:latest")),
    ])
    monkeypatch.setattr(
        client, "_candidate_installed",
        lambda spec: spec.model == "local:latest",
    )

    out = client.generate_json(
        "q", role="classification", config=cfg, api=api,
        required_capabilities={"classification", "json"}, retries=0,
        privacy="never_cloud",
    )
    assert out == {"ok": True}
    assert [req["json"]["model"] for req in api.requests] == ["local:latest"]


def test_generate_json_role_rejects_served_model_mismatch(registered_key, monkeypatch):
    """Role-path generate_json fails closed when the broker serves a different
    model than the requested candidate — a hidden fallback must not be attributed
    to the candidate's compatibility/quarantine state."""
    cfg = {"roles": {"classification": {
        "model": "local:latest", "backend": "ollama",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body('{"ok": true}', model="impostor:latest")),
    ])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json(
            "q", role="classification", config=cfg, api=api,
            required_capabilities={"classification", "json"}, retries=0,
            privacy="never_cloud",
        )


def test_generate_json_keeps_lmstudio_role_candidate_eligible(
    registered_key, monkeypatch
):
    """A loopback LM Studio role must not be filtered out of candidacy — it has no
    downloadable artifact to probe, so _candidate_installed must count it present."""
    cfg = {"roles": {"classification": {
        "model": "lmstudio-model", "backend": "lmstudio",
        "endpoint": "http://127.0.0.1:1234/v1",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body('{"ok": true}', model="lmstudio-model")),
    ])
    out = client.generate_json(
        "q", role="classification", config=cfg, api=api,
        required_capabilities={"classification", "json"}, retries=0,
        privacy="never_cloud",
    )
    assert out == {"ok": True}
    assert len(api.requests) == 1  # it made the HTTP call, not NoCompatibleModel


def test_complete_unknown_role_raises_public_exception(registered_key):
    """complete() must surface an unknown role as the public CheapskateUnavailable,
    not the internal resolver LocalUnavailable."""
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("q", role="does-not-exist", config={"roles": {}})


def test_generate_json_unknown_role_raises_public_exception(registered_key):
    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json("q", role="does-not-exist", config={"roles": {}},
                             privacy="cloud_allowed", retries=0)


class _BoomClient:
    def post(self, *a, **k):
        raise RuntimeError("no network in test")

    def close(self):
        pass


def test_never_cloud_disables_env_proxy_trust(registered_key, monkeypatch):
    """never_cloud must build the httpx client with trust_env=False so an
    env-configured HTTP(S)_PROXY/ALL_PROXY cannot tunnel the private prompt off
    the box even when the URL is loopback."""
    captured = {}

    def spy(*a, **k):
        captured.update(k)
        return _BoomClient()

    monkeypatch.setattr(client.httpx, "Client", spy)
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="concrete:latest", privacy="never_cloud")
    assert captured.get("trust_env") is False


def test_cloud_allowed_keeps_env_proxy_trust(registered_key, monkeypatch):
    captured = {}

    def spy(*a, **k):
        captured.update(k)
        return _BoomClient()

    monkeypatch.setattr(client.httpx, "Client", spy)
    with pytest.raises(client.CheapskateUnavailable):
        client.complete("hi", model="concrete:latest", privacy="cloud_allowed")
    assert captured.get("trust_env") is True


def test_typed_config_resolves_remote_role_endpoint():
    """A typed Config role with a non-default backend keeps its endpoint (the
    RoleEntry.endpoint field) instead of mis-resolving to the Ollama localhost."""
    from cheapskate.backends.resolve import resolve
    from cheapskate.config import Config

    cfg = Config.model_validate({"roles": {"r": {
        "model": "vendor/m", "backend": "remote",
        "endpoint": "https://remote.example.com/v1",
    }}})
    spec = resolve(role="r", config=cfg)
    assert spec.backend == "remote"
    assert spec.endpoint == "https://remote.example.com/v1"


def test_typed_config_backends_urls_used_for_default_endpoint():
    """default_endpoint reads a typed BackendEntry's url, not only string maps."""
    from cheapskate.backends.resolve import default_endpoint
    from cheapskate.config import Config

    cfg = Config.model_validate({"backends": {
        "remote": {"kind": "remote", "url": "https://backend.example.com/v1"},
    }})
    assert default_endpoint("remote", cfg) == "https://backend.example.com/v1"


def test_generate_json_tries_autopull_ollama_candidate(registered_key, monkeypatch):
    """With auto_pull on, a not-yet-installed Ollama incumbent is still a valid
    candidate (the broker gate-pulls it) — generate_json attempts it rather than
    raising without an HTTP call."""
    cfg = {"machine": {"auto_pull": True}, "roles": {"classification": {
        "model": "fresh:latest", "backend": "ollama",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([
        FakeResponse(200, _chat_body('{"ok": true}', model="fresh:latest")),
    ])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: False)
    out = client.generate_json(
        "q", role="classification", config=cfg, api=api,
        required_capabilities={"classification", "json"}, retries=0,
    )
    assert out == {"ok": True}
    assert [r["json"]["model"] for r in api.requests] == ["fresh:latest"]


def test_generate_json_custom_role_without_declared_capabilities(
    registered_key, monkeypatch
):
    """A user-defined role that declares no capabilities must still resolve — it
    is assumed to satisfy the caller's required capabilities, not filtered out."""
    cfg = {"roles": {"my_role": {"model": "custom:latest", "backend": "ollama"}}}
    api = FakeClient([
        FakeResponse(200, _chat_body('{"ok": true}', model="custom:latest")),
    ])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)
    out = client.generate_json("q", role="my_role", config=cfg, api=api, retries=0)
    assert out == {"ok": True}


def test_role_entry_loads_declared_capabilities():
    """RoleEntry must preserve a declared capabilities list (Pydantic previously
    stripped the unknown YAML key, emptying custom-role capabilities)."""
    from cheapskate.config import Config

    cfg = Config.model_validate({"roles": {"my_role": {
        "model": "custom:latest", "backend": "ollama",
        "capabilities": ["text", "json", "vision"],
    }}})
    assert cfg.roles["my_role"].capabilities == ["text", "json", "vision"]


def test_generate_json_rejects_undeclared_requested_capability(registered_key, monkeypatch):
    cfg = {"roles": {"classification": {
        "model": "text-only:latest", "backend": "ollama",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}'))])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json(
            "q", role="classification", config=cfg, api=api,
            required_capabilities={"vision", "json"}, retries=0,
        )
    assert api.requests == []


def test_generate_json_skips_uninstalled_incumbent(registered_key, monkeypatch):
    # auto_pull off so an uninstalled Ollama incumbent is genuinely unavailable
    # (with auto_pull on it would be a valid gate-pull candidate — see the
    # dedicated auto_pull test below).
    cfg = {"machine": {"auto_pull": False}, "roles": {"classification": {
        "model": "missing:latest", "backend": "ollama",
        "fallback": "present:latest",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}', model="present:latest"))])
    monkeypatch.setattr(
        client, "_candidate_installed", lambda spec: spec.model == "present:latest"
    )

    out = client.generate_json(
        "q", role="classification", config=cfg, api=api,
        required_capabilities={"classification", "json"}, retries=0,
    )
    assert out == {"ok": True}
    assert [request["json"]["model"] for request in api.requests] == ["present:latest"]


def test_generate_json_never_cloud_rejects_remote_endpoint(
    registered_key, monkeypatch
):
    cfg = {"roles": {"classification": {
        "model": "remote:latest",
        "backend": "ollama",
        "endpoint": "https://models.example.com/v1",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}'))])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json(
            "q", role="classification", config=cfg, api=api,
            required_capabilities={"classification", "json"}, retries=0,
            privacy="never_cloud",
        )
    assert api.requests == []


@pytest.mark.parametrize(
    "routing", [{"role": "classification"}, {"model": "local:latest"}],
)
def test_generate_json_never_cloud_rejects_remote_broker_before_http(
    registered_key, monkeypatch, routing
):
    monkeypatch.setenv("CHEAPSKATE_BROKER_URL", "https://remote.example.com")
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}'))])
    with pytest.raises(client.CheapskateUnavailable, match="loopback broker"):
        client.generate_json(
            "private", api=api, privacy="never_cloud", retries=0, **routing,
        )
    assert api.requests == []


@pytest.mark.parametrize("backend", ["cloud", "remote"])
@pytest.mark.parametrize("routing", [{"role": "classification"}, {"model": "gateway-model"}])
def test_generate_json_never_cloud_rejects_nonlocal_backend_on_loopback(
    registered_key, monkeypatch, backend, routing
):
    cfg = {"roles": {"classification": {
        "model": "gateway-model",
        "backend": backend,
        "endpoint": "http://127.0.0.1:4000/v1",
        "capabilities": ["text", "classification", "json"],
    }}}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}'))])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    with pytest.raises(client.CheapskateUnavailable, match="verified local backend"):
        client.generate_json(
            "private", config=cfg, api=api, privacy="never_cloud", retries=0,
            **routing,
        )
    assert api.requests == []


def test_generate_json_never_cloud_rejects_rollback_snapshot_nonlocal_endpoint(
    registered_key, monkeypatch
):
    """H2 trust: a rollback snapshot carrying a NONLOCAL endpoint is refused by the
    existing never_cloud gates — a poisoned snapshot cannot smuggle a cloud route
    into a never_cloud call. No bespoke snapshot validation is needed."""
    cfg = {"roles": {"classification": {
        "model": "org/current", "backend": "ollama",
        "capabilities": ["text", "classification", "json"],
        "rollback": ["vendor/former"],
        "rollback_configs": {"vendor/former": {
            "backend": "lmstudio",
            "endpoint": "https://models.example.com/v1",
        }},
    }}}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}', model="vendor/former"))])
    monkeypatch.setattr(client, "_candidate_installed", lambda _spec: True)

    with pytest.raises(client.CheapskateUnavailable, match="verified local backend"):
        client.generate_json(
            "private", model="vendor/former", config=cfg, api=api,
            privacy="never_cloud", retries=0,
        )
    assert api.requests == []


def test_generate_json_never_cloud_rejects_ambiguous_remote_fallback(
    registered_key, monkeypatch
):
    # auto_pull off so the uninstalled local incumbent is genuinely unavailable
    # and the only installed candidate is the ambiguous remote fallback.
    cfg = {"machine": {"auto_pull": False}, "roles": {
        "classification": {
            "model": "missing-local:latest", "backend": "ollama",
            "fallback": "shared-model",
            "capabilities": ["text", "classification", "json"],
        },
        "remote-role": {
            "model": "shared-model", "backend": "remote",
            "endpoint": "https://models.example.com/v1",
        },
    }}
    api = FakeClient([FakeResponse(200, _chat_body('{"ok": true}'))])
    monkeypatch.setattr(
        client, "_candidate_installed",
        lambda spec: spec.model == "shared-model",
    )

    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json(
            "private", role="classification", config=cfg, api=api,
            required_capabilities={"classification", "json"}, retries=0,
            privacy="never_cloud",
        )
    assert api.requests == []
