# SPDX-License-Identifier: Apache-2.0
"""The broker's OpenAI-compatible /v1/chat/completions adoption surface (S3).

The valuable logic — task_type routing, cloud dispatch + OpenAI shaping, and the
fail-closed safety-class refusals — lives in module-level functions
(``plan_task_type_route``, ``cloud_dispatch_openai``, and the payload helpers) so
it is unit-testable without spinning up a live ASGI server (per the repo's
no-live-servers test rule). The cloud dispatch is injected — no network, no key.
"""

from __future__ import annotations

from cheapskate.broker import app
from cheapskate.config import Config, ProviderConfig


class _Result:
    def __init__(self, text="cloud reply", model="gpt-x", tin=3, tout=5):
        self.text, self.model, self.tokens_in, self.tokens_out = text, model, tin, tout


def _cloud_config():
    return Config(
        providers={
            "cloud": ProviderConfig(
                kind="openai-compat", base_url="https://x/v1",
                model_map={"reasoning": "gpt-x"}, api_key_env="K", enabled=True,
            )
        },
    )


def _cloudy(config):
    """Add a cloud-only task_type so routing goes cloud regardless of dial."""
    config.task_types["cloudy"] = config.task_types["summarize"].model_copy(
        update={"route": "cloud-only"}
    )
    return config


# ── OpenAI-payload helpers ───────────────────────────────────────────────────


def test_last_user_text_picks_latest_user_message():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert app._last_user_text(msgs) == "second"


def test_last_user_text_handles_content_parts():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "part"}]}]
    assert app._last_user_text(msgs) == "part"


def test_system_text_joins_system_messages():
    msgs = [{"role": "system", "content": "a"}, {"role": "user", "content": "u"}]
    assert app._system_text(msgs) == "a"
    assert app._system_text([{"role": "user", "content": "u"}]) is None


def test_openai_chat_shape():
    shape = app._openai_chat_shape(_Result(text="hi", tin=2, tout=4), "summarize")
    assert shape["object"] == "chat.completion"
    assert shape["choices"][0]["message"] == {"role": "assistant", "content": "hi"}
    assert shape["usage"]["total_tokens"] == 6


# ── plan_task_type_route: safety classes fail closed ─────────────────────────


def test_plan_never_local_refuses_422(tmp_path):
    cfg = Config(never_local=["financial"])
    plan = app.plan_task_type_route(cfg, "financial", dial=(2, "std"))
    assert plan["action"] == "refuse"
    assert plan["status"] == 422
    assert plan["class"] == "never_local"


def test_plan_never_cloud_at_level_0_refuses_422():
    cfg = Config(never_cloud=["secrets"])
    plan = app.plan_task_type_route(cfg, "secrets", dial=(0, None))
    assert plan["action"] == "refuse"
    assert plan["status"] == 422
    assert plan["class"] == "never_cloud"


def test_plan_never_cloud_forced_local():
    cfg = Config(never_cloud=["secrets"])
    plan = app.plan_task_type_route(cfg, "secrets", dial=(2, "std"))
    assert plan["action"] == "local"


def test_plan_cloud_only_routes_cloud():
    cfg = _cloudy(_cloud_config())
    plan = app.plan_task_type_route(cfg, "cloudy", dial=(2, "std"))
    assert plan["action"] == "cloud"
    assert plan["decision"]["role"] == "reasoning"


def test_plan_local_route_pins_role_model():
    cfg = Config()
    plan = app.plan_task_type_route(cfg, "summarize", dial=(2, "std"))
    assert plan["action"] == "local"
    assert plan["model"] == "role:reasoning"


# ── cloud_dispatch_openai: shaping + fail-closed ─────────────────────────────


def test_cloud_dispatch_returns_openai_shape():
    cfg = _cloud_config()
    decision = {"role": "reasoning"}
    body = {"messages": [{"role": "user", "content": "hello"}]}
    status, out = app.cloud_dispatch_openai(
        cfg, body, decision, "cloudy", max_tokens_floor=4096,
        dispatch=lambda *a, **k: _Result(text="cloud reply", model="gpt-x", tin=3, tout=5),
    )
    assert status == 200
    assert out["choices"][0]["message"]["content"] == "cloud reply"
    assert out["model"] == "gpt-x"
    assert out["usage"]["total_tokens"] == 8


def test_cloud_dispatch_no_provider_is_502():
    # default config has no enabled provider → dispatch_role raises CloudError → 502
    cfg = Config()
    status, out = app.cloud_dispatch_openai(
        cfg, {"messages": [{"role": "user", "content": "x"}]},
        {"role": "reasoning"}, "cloudy", max_tokens_floor=4096,
    )
    assert status == 502
    assert "provider" in out["error"].lower()


def test_cloud_dispatch_provider_failure_is_502():
    from cheapskate.cloud import CloudError

    cfg = _cloud_config()

    def boom(*a, **k):
        raise CloudError("rate limited")

    status, out = app.cloud_dispatch_openai(
        cfg, {"messages": [{"role": "user", "content": "x"}]},
        {"role": "reasoning"}, "cloudy", max_tokens_floor=4096, dispatch=boom,
    )
    assert status == 502
    assert "rate limited" in out["error"]


def test_cloud_dispatch_threads_system_and_prompt():
    cfg = _cloud_config()
    seen = {}

    def dispatch(config, role, prompt, *, system=None, temperature=0.2, max_tokens=0):
        seen.update(role=role, prompt=prompt, system=system, max_tokens=max_tokens)
        return _Result()

    body = {
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "the question"},
        ],
        "max_tokens": 256,
    }
    app.cloud_dispatch_openai(cfg, body, {"role": "reasoning"}, "cloudy",
                              max_tokens_floor=4096, dispatch=dispatch)
    assert seen["role"] == "reasoning"
    assert seen["prompt"] == "the question"
    assert seen["system"] == "be brief"
    assert seen["max_tokens"] == 256
