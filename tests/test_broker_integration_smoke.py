# SPDX-License-Identifier: Apache-2.0
"""In-process TestClient smoke of the real FastAPI broker app.

This is the regression test for the S3 finding: with ``from __future__ import
annotations`` a route's ``request: Request`` annotation is a STRING that FastAPI
must resolve against the module globals. When ``Request`` was a function-local
import that resolution failed and EVERY route 422'd ("query.request missing").
Binding ``Request`` at module scope fixes it — and this test proves it end to end
by driving the built app through Starlette's in-process TestClient (no live
server, no network, per the repo's test rules — TestClient runs the ASGI app in
the same process with a fake backend injected).

Skips cleanly if FastAPI/httpx are not installed (they are core deps, so in a
normal ``pip install -e .[dev]`` this always runs).
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from cheapskate.broker import app as broker_app  # noqa: E402
from cheapskate.broker.gates import keys_path  # noqa: E402
from cheapskate.config import Config  # noqa: E402


def _config_with_role() -> Config:
    """A config with a resolvable local role so /v1/models lists something and a
    concrete role:reasoning request resolves to an ollama backend."""
    cfg = Config()
    # A ``roles`` attribute short-circuits resolve() to this table (see
    # backends.resolve._roles), so we never touch a real registry.yaml.
    object.__setattr__(cfg, "roles", {"reasoning": {"model": "test-model", "backend": "ollama"}})
    return cfg


def _register_key(cfg: Config, key: str = "sk-test", cls: str = "interactive") -> None:
    kp = keys_path(cfg)
    kp.parent.mkdir(parents=True, exist_ok=True)
    kp.write_text(json.dumps({key: {"class": cls, "user": "tester"}}))


class _FakeResp:
    """Minimal httpx-response stand-in for the injected backend client."""

    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {"content-type": "application/json"}


class _FakeBackendClient:
    """Replaces the broker's httpx.AsyncClient so no real backend is hit."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.seen: list[dict] = []

    async def post(self, url, json=None, **kwargs):
        self.seen.append({"url": url, "json": json})
        return _FakeResp(200, (json_dumps(self._payload)).encode())

    async def aclose(self):
        pass


def json_dumps(obj) -> str:
    return json.dumps(obj)


@pytest.fixture
def client(monkeypatch):
    cfg = _config_with_role()
    _register_key(cfg)

    # Neutralize the capacity + backend-prepare steps so nothing touches a real
    # ollama/mlx process; the fake client below stands in for the backend HTTP.
    monkeypatch.setattr(broker_app, "enforce_capacity", lambda spec, budget: ("ok", "test"))
    monkeypatch.setattr(
        broker_app, "prepare_backend",
        lambda spec, budget, config=None: "http://backend.test/v1",
    )

    app = broker_app.build_app(cfg)
    fake = _FakeBackendClient(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "pong"},
                 "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    app.state.client = fake
    tc = TestClient(app)
    tc._fake = fake  # expose for assertions
    return tc


# ── the smoke ────────────────────────────────────────────────────────────────


def test_models_endpoint_200_with_valid_key_and_lists_roles(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer sk-test"})
    assert r.status_code == 200  # NOT 422 — the S3 regression guard
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert "role:reasoning" in ids


def test_models_endpoint_rejects_missing_key_401_not_422(client):
    r = client.get("/v1/models")
    # The S3 bug made this 422 (FastAPI treated `request` as a missing query
    # param). The correct behavior is 401 from our auth check.
    assert r.status_code == 401
    assert "key" in r.json()["error"].lower()


def test_models_endpoint_rejects_bad_key_401(client):
    r = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_chat_completions_proxies_to_fake_backend(client):
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"model": "role:reasoning", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "pong"
    # the request actually reached the injected backend
    assert client._fake.seen
    assert client._fake.seen[0]["url"].endswith("/chat/completions")


def test_chat_completions_rewrites_role_prefix_to_concrete_model(client):
    """D1 regression: a ``role:<name>`` model MUST be resolved to the role's
    concrete tag before the request reaches the backend. Before the fix,
    ``resolve()`` left ``role:reasoning`` as a literal id and the backend 404'd.
    The downstream payload the fake backend sees must carry ``test-model``, never
    ``role:reasoning``."""
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"model": "role:reasoning", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    sent = client._fake.seen[-1]["json"]
    assert sent["model"] == "test-model", f"role: prefix not decoded; sent {sent['model']!r}"
    assert not str(sent["model"]).startswith("role:")


def test_chat_completions_auth_rejection_path(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "role:reasoning", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 401


def test_d2_prepare_backend_gate_pulls_absent_ollama_model(monkeypatch):
    """D2: the broker's prepare_backend must route Ollama specs through
    ensure_role (which gate-pulls an absent model), not short-circuit and let
    the daemon 404. Assert ensure_role is called for an ollama spec."""
    from cheapskate.backends.resolve import BackendSpec

    seen = {}

    def fake_ensure_role(*, model, config, budget_gb):
        seen["model"] = model
        return BackendSpec(model=model, backend="ollama",
                           endpoint="http://127.0.0.1:11434", role="code")

    monkeypatch.setattr(broker_app, "ensure_role", fake_ensure_role)
    spec = BackendSpec(model="qwen3:4b", backend="ollama",
                       endpoint="http://127.0.0.1:11434", role="code")
    url = broker_app.prepare_backend(spec, budget_gb=64.0, config=Config())
    assert seen["model"] == "qwen3:4b"  # ensure_role WAS called (gate-pull reachable)
    assert url.endswith("/v1")


def test_d7_ensure_local_key_mints_interactive_key_on_first_run():
    """D7: a fresh install must be able to talk to its own broker. serve()
    provisions an interactive-class key mode-600 on first run; the client then
    finds it (client and broker share the real keys_path, resolved from the
    XDG state dir the conftest isolates to a tmp dir). A second call is
    idempotent (no duplicate key)."""
    from cheapskate import client as client_mod
    from cheapskate.broker.gates import keys_path

    cfg = Config()
    keyfile = keys_path(cfg)  # resolves under the isolated tmp state dir

    key, created = broker_app.ensure_local_key(cfg)
    assert created is True
    data = json.loads(keyfile.read_text())
    assert data[key]["class"] == "interactive"
    assert (keyfile.stat().st_mode & 0o777) == 0o600  # mode-600
    # the client finds the SAME key through the real shared keys_path
    assert client_mod._api_key(cfg) == key
    # idempotent: a second call does not add a key
    key2, created2 = broker_app.ensure_local_key(cfg)
    assert created2 is False and key2 == key
    assert len(json.loads(keyfile.read_text())) == 1


def test_d8_internal_call_emits_no_generation_event(client, monkeypatch):
    """D8: a request from cheapskate's own router (X-Cheapskate-Internal) must NOT
    produce a broker `generation` event (the router already emitted the costable
    one), only an ops `broker.serve` record. This prevents the econ double-count."""
    import cheapskate.telemetry as tele

    events = []
    monkeypatch.setattr(tele, "log_event", lambda kind, **f: events.append((kind, f)))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test", "X-Cheapskate-Internal": "1"},
        json={"model": "role:reasoning", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    kinds = [k for k, _ in events]
    assert "broker.serve" in kinds          # ops record present
    assert "generation" not in kinds        # NO cost event (router owns it)


def test_d8_external_call_emits_one_clean_generation_event(client, monkeypatch):
    """D8: genuinely external OpenAI-compat traffic (no internal header) has no
    router meter, so the broker IS its meter: exactly one `generation` event with
    a clean route='local' and the task_type, plus the ops record."""
    import cheapskate.telemetry as tele

    events = []
    monkeypatch.setattr(tele, "log_event", lambda kind, **f: events.append((kind, f)))
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test", "X-Task-Type": "summarize"},
        json={"model": "role:reasoning", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200
    gens = [f for k, f in events if k == "generation"]
    assert len(gens) == 1
    assert gens[0]["route"] == "local"        # clean bucket, not "ollama:reasoning"
    assert gens[0]["task_type"] == "summarize"
    assert gens[0]["retries"] == 0 and gens[0]["escalated"] is False
    assert any(k == "broker.serve" for k, _ in events)


def test_stream_with_task_type_is_400_invalid_request(client):
    # R1: streaming through the task_type econ path is unsupported. It must be a
    # 400 invalid_request_error (OpenAI clients handle it gracefully), NOT a 501
    # (which reads as endpoint-fatal).
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={
            "task_type": "summarize", "stream": True,
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "stream_not_supported"
