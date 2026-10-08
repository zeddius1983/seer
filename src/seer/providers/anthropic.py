"""
Anthropic Messages API provider.
"""

import json
import os
from typing import Iterator

import httpx

from .base import Provider
from ..config import ProviderConfig

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.model = cfg.model
        self.base_url = (cfg.base_url or ANTHROPIC_BASE_URL).rstrip("/")
        self.api_key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self.effort = cfg.reasoning_effort

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
            "max_tokens": 16000,
            "stream": True,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.effort:
            payload["output_config"] = {"effort": self.effort}

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
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield delta.get("text", "")
                    except (json.JSONDecodeError, KeyError):
                        continue
