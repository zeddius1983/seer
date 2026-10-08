"""Tests for the request bodies the OpenAI and Anthropic providers send."""

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from seer.config import ProviderConfig
from seer.providers.anthropic import AnthropicProvider
from seer.providers.openai import OpenAIProvider


def _sent_payload(provider, module):
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            return iter(())

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
        list(provider.stream("system", "prompt"))
    return sent


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
