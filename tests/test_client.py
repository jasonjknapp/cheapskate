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
    # Routed to the broker chat endpoint, with a role: model field + bearer key.
    req = api.requests[0]
    assert req["url"].endswith("/v1/chat/completions")
    assert req["json"]["model"] == "role:reasoning"
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
        "role:reasoning", "fallback:latest",
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
        "role:reasoning", "fallback:latest",
    ]


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
    api = FakeClient([FakeResponse(200, _chat_body('{"fruit": "apple"}'))])
    out = client.generate_json("list a fruit", model="m", api=api)
    assert out == {"fruit": "apple"}
    # Structured requests set response_format json_object.
    assert api.requests[0]["json"]["response_format"] == {"type": "json_object"}


def test_generate_json_repairs_then_succeeds(registered_key):
    api = FakeClient([
        FakeResponse(200, _chat_body("not json at all")),
        FakeResponse(200, _chat_body('{"ok": true}')),
    ])
    out = client.generate_json("q", model="m", api=api, retries=2)
    assert out == {"ok": True}
    assert len(api.requests) == 2  # one repair round
    # The repair nudge was appended to the conversation.
    second_msgs = api.requests[1]["json"]["messages"]
    assert any("valid JSON" in m["content"] for m in second_msgs if m["role"] == "user")


def test_generate_json_exhausts_retries_and_degrades(registered_key):
    api = FakeClient([FakeResponse(200, _chat_body("garbage")) for _ in range(3)])
    with pytest.raises(client.CheapskateUnavailable):
        client.generate_json("q", model="m", api=api, retries=2)
    assert len(api.requests) == 3  # retries + 1


def test_generate_json_validates_pydantic_schema(registered_key):
    pydantic = pytest.importorskip("pydantic")

    class Fruit(pydantic.BaseModel):
        name: str
        qty: int

    api = FakeClient([FakeResponse(200, _chat_body('{"name": "pear", "qty": 3}'))])
    out = client.generate_json("q", schema=Fruit, model="m", api=api)
    assert out == {"name": "pear", "qty": 3}


def test_generate_json_repairs_valid_json_with_wrong_schema_root(registered_key):
    api = FakeClient([
        FakeResponse(200, _chat_body('[{"items": []}]')),
        FakeResponse(200, _chat_body('{"items": []}')),
    ])
    schema = {"type": "object", "required": ["items"],
              "properties": {"items": {"type": "array"}}}
    out = client.generate_json("q", schema=schema, model="m", api=api, retries=1)
    assert out == {"items": []}
    assert len(api.requests) == 2


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
    cfg = {"roles": {"classification": {
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


def test_generate_json_never_cloud_rejects_ambiguous_remote_fallback(
    registered_key, monkeypatch
):
    cfg = {"roles": {
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
