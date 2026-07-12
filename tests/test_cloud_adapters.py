# SPDX-License-Identifier: Apache-2.0
"""Cloud adapters: provider selection, env-only secrets, uniform result shape,
lazy-extra errors, and the fail-closed 'no enabled provider' path. No network,
no real keys — the SDK client is injected."""

from __future__ import annotations

import pytest

from cheapskate.cloud import CloudError, adapters
from cheapskate.config import Config, ProviderConfig


def _has(mod: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(mod) is not None


# These two cases exercise the REAL SDK client-construction path (no injected
# client), so they only make sense when the SDK extra is installed. A bare
# ``pip install -e .[dev]`` (no optional extras) skips them; CI installs the
# extras so they still run there. Every other test in this file injects a fake
# client and needs no SDK.
requires_openai = pytest.mark.skipif(
    not _has("openai"), reason="needs the 'openai' extra (pip install 'cheapskate[openai]')"
)


# ── fake SDK clients (injected) ──────────────────────────────────────────────


class FakeOpenAIClient:
    """Mimics the openai SDK surface used by the adapter."""

    def __init__(self, content="cloud says hi", model="gpt-x", pin=3, pout=7):
        self._content, self._model, self._pin, self._pout = content, model, pin, pout
        self.calls = []

        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Resp(outer._content, outer._model, outer._pin, outer._pout)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    def __init__(self, pin, pout):
        self.prompt_tokens, self.completion_tokens = pin, pout
        self.input_tokens, self.output_tokens = pin, pout


class _Resp:
    def __init__(self, content, model, pin, pout):
        self.choices = [_Choice(content)]
        self.model = model
        self.usage = _Usage(pin, pout)


class FakeAnthropicClient:
    def __init__(self, text="claude says hi", model="claude-x", pin=4, pout=9):
        self._text, self._model, self._pin, self._pout = text, model, pin, pout
        self.calls = []

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _AResp(outer._text, outer._model, outer._pin, outer._pout)

        self.messages = _Messages()


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _AResp:
    def __init__(self, text, model, pin, pout):
        self.content = [_Block(text)]
        self.model = model
        self.usage = _Usage(pin, pout)


# ── config helpers ───────────────────────────────────────────────────────────


def _cfg(**providers):
    return Config(providers=providers)


def _openai_provider(enabled=True):
    return ProviderConfig(
        kind="openai-compat", base_url="https://api.example/v1",
        model_map={"reasoning": "gpt-x", "code": "gpt-code"},
        api_key_env="EXAMPLE_KEY", enabled=enabled,
    )


def _anthropic_provider(enabled=True):
    return ProviderConfig(
        kind="anthropic", model_map={"reasoning": "claude-x"},
        api_key_env="ANTHROPIC_KEY", enabled=enabled,
    )


# ── provider selection ───────────────────────────────────────────────────────


def test_no_enabled_provider_is_hard_error():
    cfg = _cfg(cloud=_openai_provider(enabled=False))
    with pytest.raises(CloudError) as e:
        adapters.provider_for_role(cfg, "reasoning")
    assert "no provider is enabled" in str(e.value)


def test_unmapped_role_is_hard_error():
    cfg = _cfg(cloud=_openai_provider())
    with pytest.raises(CloudError) as e:
        adapters.provider_for_role(cfg, "vision")  # not in model_map
    assert "vision" in str(e.value)


def test_provider_for_role_returns_mapped_model():
    cfg = _cfg(cloud=_openai_provider())
    name, provider, model_id = adapters.provider_for_role(cfg, "code")
    assert name == "cloud"
    assert provider.kind == "openai-compat"
    assert model_id == "gpt-code"


def test_selection_is_deterministic_sorted_by_name():
    # two enabled providers, both map reasoning; sorted-name order wins ("a" < "b")
    cfg = _cfg(bbb=_anthropic_provider(), aaa=_openai_provider())
    name, _, model_id = adapters.provider_for_role(cfg, "reasoning")
    assert name == "aaa"
    assert model_id == "gpt-x"


# ── secrets are env-only ─────────────────────────────────────────────────────


@requires_openai
def test_missing_api_key_env_is_hard_error(monkeypatch):
    monkeypatch.delenv("EXAMPLE_KEY", raising=False)
    cfg = _cfg(cloud=_openai_provider())
    with pytest.raises(CloudError) as e:
        # no injected client → the adapter must read the key from env and fail
        adapters.dispatch_role(cfg, "reasoning", "hi")
    assert "EXAMPLE_KEY" in str(e.value)


@requires_openai
def test_no_api_key_env_configured_is_hard_error():
    prov = ProviderConfig(kind="openai-compat", model_map={"reasoning": "m"},
                          api_key_env=None, enabled=True)
    with pytest.raises(CloudError) as e:
        adapters.dispatch_provider(prov, "m", "hi")
    assert "api_key_env" in str(e.value)


# ── dispatch (injected client) → uniform result shape ────────────────────────


def test_openai_compat_dispatch_result_shape():
    cfg = _cfg(cloud=_openai_provider())
    fake = FakeOpenAIClient(content="the answer", model="gpt-x", pin=11, pout=22)
    res = adapters.dispatch_role(cfg, "reasoning", "question", system="be terse", client=fake)
    assert res.text == "the answer"
    assert res.model == "gpt-x"
    assert res.tokens_in == 11
    assert res.tokens_out == 22
    assert res.latency_s >= 0.0
    # system + user threaded through as messages
    msgs = fake.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "question"}
    assert fake.calls[0]["model"] == "gpt-x"


def test_anthropic_dispatch_result_shape():
    cfg = _cfg(cloud=_anthropic_provider())
    fake = FakeAnthropicClient(text="claude answer", model="claude-x", pin=5, pout=6)
    res = adapters.dispatch_role(cfg, "reasoning", "q", system="sys", client=fake)
    assert res.text == "claude answer"
    assert res.model == "claude-x"
    assert res.tokens_in == 5
    assert res.tokens_out == 6
    # anthropic takes system as a top-level kwarg, not a message
    assert fake.calls[0]["system"] == "sys"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "q"}]


def test_empty_content_is_hard_error():
    cfg = _cfg(cloud=_openai_provider())
    fake = FakeOpenAIClient(content="")
    with pytest.raises(CloudError):
        adapters.dispatch_role(cfg, "reasoning", "q", client=fake)


def test_provider_exception_wrapped_as_cloud_error():
    cfg = _cfg(cloud=_openai_provider())

    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**k):
                    raise RuntimeError("rate limited")

    with pytest.raises(CloudError) as e:
        adapters.dispatch_role(cfg, "reasoning", "q", client=Boom())
    assert "rate limited" in str(e.value)


def test_unknown_provider_kind_is_hard_error():
    prov = ProviderConfig(kind="mystery", model_map={"reasoning": "m"},
                          api_key_env="K", enabled=True)
    with pytest.raises(CloudError) as e:
        adapters.dispatch_provider(prov, "m", "q", client=object())
    assert "unknown provider kind" in str(e.value)


# ── plain-dict responses (tolerant shape helpers) ────────────────────────────


def test_openai_text_tolerates_dict_response():
    class DictClient:
        class chat:
            class completions:
                @staticmethod
                def create(**k):
                    return {
                        "model": "m",
                        "choices": [{"message": {"content": "dict content"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                    }

    prov = _openai_provider()
    res = adapters.dispatch_provider(prov, "m", "q", client=DictClient())
    assert res.text == "dict content"
    assert res.tokens_in == 1
    assert res.tokens_out == 2
