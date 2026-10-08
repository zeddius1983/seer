"""Tests for the request bodies the OpenAI and Anthropic providers send."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from seer.config import ProviderConfig
from seer.providers.anthropic import AnthropicProvider
from seer.providers.openai import OpenAIProvider


def _sent_payload(provider, module, lines=()):
    sent = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self):
            return iter(lines)

    class _Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @contextmanager
        def stream(self, method, url, headers, json):
            sent.update(json)
            yield _Resp()

    with patch(f"seer.providers.{module}.httpx.Client", _Client):
        sent["_yielded"] = list(provider.stream("system", "prompt"))
    return sent


def _sse(*objects):
    import json
    return [f"data: {json.dumps(o)}" for o in objects] + ["data: [DONE]"]


@pytest.mark.parametrize("effort", [None, "low"])
def test_anthropic_effort_only_when_configured(effort):
    cfg = ProviderConfig(type="anthropic", model="claude-sonnet-5-5", reasoning_effort=effort)
    payload = _sent_payload(AnthropicProvider(cfg), "anthropic")
    assert payload.get("output_config") == ({"effort": effort} if effort else None)
    assert payload["max_tokens"] >= 16000   # thinking counts against it


@pytest.mark.parametrize("effort", [None, "low"])
def test_openai_effort_only_when_configured(effort):
    cfg = ProviderConfig(type="openai", model="gpt-6.1-sol", reasoning_effort=effort)
    payload = _sent_payload(OpenAIProvider(cfg), "openai")
    assert payload.get("reasoning_effort") == effort


@pytest.mark.parametrize("base_url, key", [
    ("http://localhost:8000/v1", "max_tokens"),       # llama.cpp, vLLM, Ollama…
    (None, "max_completion_tokens"),                   # OpenAI: reasoning models reject max_tokens
])
def test_openai_caps_output(base_url, key):
    cfg = ProviderConfig(type="openai", model="m", base_url=base_url)
    payload = _sent_payload(OpenAIProvider(cfg), "openai")
    assert payload[key] == 16000
    other = {"max_tokens", "max_completion_tokens"} - {key}
    assert not other & set(payload)


def test_output_cap_is_configurable():
    cfg = ProviderConfig(type="openai", model="m", base_url="http://x/v1", max_tokens=4000)
    assert _sent_payload(OpenAIProvider(cfg), "openai")["max_tokens"] == 4000
    cfg = ProviderConfig(type="anthropic", model="claude-sonnet-5-5", max_tokens=4000)
    assert _sent_payload(AnthropicProvider(cfg), "anthropic")["max_tokens"] == 4000


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])   # llama.cpp / Ollama
def test_openai_collects_reasoning_but_yields_only_the_answer(field):
    provider = OpenAIProvider(ProviderConfig(type="openai", model="m", base_url="http://x/v1"))
    lines = _sse(
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [{"delta": {field: "The user wants "}}]},
        {"choices": [{"delta": {field: "a number."}}]},
        {"choices": [{"delta": {"content": "391"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )
    payload = _sent_payload(provider, "openai", lines)
    assert payload["_yielded"] == ["391"]
    assert provider.reasoning == "The user wants a number."


def test_openai_reasoning_past_the_cap_is_a_clear_error():
    provider = OpenAIProvider(ProviderConfig(
        type="openai", model="m", base_url="http://x/v1", name="vllm", max_tokens=20))
    lines = _sse(
        {"choices": [{"delta": {"reasoning_content": "Let me think about this "}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    )
    with pytest.raises(RuntimeError, match=r"vllm: the model used its whole output limit \(20 tokens\) reasoning"):
        _sent_payload(provider, "openai", lines)


def test_openai_truncated_answer_is_kept():
    provider = OpenAIProvider(ProviderConfig(type="openai", model="m", base_url="http://x/v1"))
    lines = _sse(
        {"choices": [{"delta": {"content": "Use ls"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
    )
    assert _sent_payload(provider, "openai", lines)["_yielded"] == ["Use ls"]


def test_anthropic_thinking_then_cap_is_a_clear_error():
    provider = AnthropicProvider(ProviderConfig(type="anthropic", model="claude-sonnet-5-5", name="anthropic"))
    lines = [f"data: {line}" for line in (
        '{"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Considering"}}',
        '{"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}',
    )]
    with pytest.raises(RuntimeError, match="output limit"):
        _sent_payload(provider, "anthropic", lines)
    assert provider.reasoning == "Considering"


class _ServerSequence:
    """httpx.Client stand-in answering successive requests from `replies`:
    (status, body or SSE lines). Records each request's payload."""

    def __init__(self, replies):
        self.replies, self.payloads = list(replies), []

    def client(self, **kwargs):
        outer = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            @contextmanager
            def stream(self, method, url, headers, json):
                outer.payloads.append(dict(json))
                status, content = outer.replies.pop(0)

                class _Resp:
                    status_code = status

                    def read(self):
                        return content.encode()

                    def raise_for_status(self):
                        if status >= 400:
                            import httpx
                            raise httpx.HTTPStatusError(f"{status}", request=None, response=None)

                    def iter_lines(self):
                        return iter(content if isinstance(content, list) else [])

                yield _Resp()

        return _Client()


_VLLM_TOO_LARGE = ('{"object": "error", "message": "This model\'s maximum context length is 8192 tokens. '
                   'However, you requested 16100 tokens (100 in the messages, 16000 in the completion)."}')


def test_default_cap_rejected_by_a_small_context_is_dropped():
    server = _ServerSequence([
        (400, _VLLM_TOO_LARGE),
        (200, _sse({"choices": [{"delta": {"content": "Use ls"}}]})),
        (200, _sse({"choices": [{"delta": {"content": "again"}}]})),
    ])
    provider = OpenAIProvider(ProviderConfig(type="openai", model="m", base_url="http://vllm:8000/v1"))
    with patch("seer.providers.openai.httpx.Client", server.client):
        assert list(provider.stream("S", "P")) == ["Use ls"]
        assert list(provider.stream("S", "P2")) == ["again"]   # e.g. the next brave step
    assert [("max_tokens" in p) for p in server.payloads] == [True, False, False]


def test_other_bad_requests_still_fail():
    server = _ServerSequence([(400, '{"error": "invalid model"}')])
    provider = OpenAIProvider(ProviderConfig(type="openai", model="m", base_url="http://x/v1"))
    import httpx
    with patch("seer.providers.openai.httpx.Client", server.client), pytest.raises(httpx.HTTPStatusError):
        list(provider.stream("S", "P"))
    assert len(server.payloads) == 1


def test_configured_cap_is_never_dropped():
    server = _ServerSequence([(400, _VLLM_TOO_LARGE)])
    provider = OpenAIProvider(ProviderConfig(type="openai", model="m", base_url="http://x/v1", max_tokens=12000))
    import httpx
    with patch("seer.providers.openai.httpx.Client", server.client), pytest.raises(httpx.HTTPStatusError):
        list(provider.stream("S", "P"))
    assert server.payloads[0]["max_tokens"] == 12000 and len(server.payloads) == 1
