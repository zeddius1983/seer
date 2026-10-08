"""
Anthropic Messages API provider.
"""

import json
import os
from typing import Iterator

import httpx

from .base import Provider, output_limit_error
from ..config import DEFAULT_MAX_TOKENS, ProviderConfig

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.model = cfg.model
        self.base_url = (cfg.base_url or ANTHROPIC_BASE_URL).rstrip("/")
        self.api_key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self.effort = cfg.reasoning_effort
        self.max_tokens = cfg.max_tokens or DEFAULT_MAX_TOKENS
        self.name = cfg.name or "anthropic"

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            # Room for thinking as well as the answer: current models think by
            # default, and thinking counts against max_tokens.
            "max_tokens": self.max_tokens,
            "stream": True,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.effort:
            payload["output_config"] = {"effort": self.effort}

        self.reasoning = ""
        answered = False
        stop_reason = None
        with httpx.Client(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/v1/messages",
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta":
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            answered = True
                            yield delta["text"]
                        elif delta.get("type") == "thinking_delta":
                            self.reasoning += delta.get("thinking") or ""
                    elif event.get("type") == "message_delta":
                        stop_reason = delta.get("stop_reason") or stop_reason
        if stop_reason == "max_tokens" and not answered:
            raise output_limit_error(self.name, self.max_tokens, bool(self.reasoning))
