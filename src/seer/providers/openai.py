"""
OpenAI-compatible provider.
Works with: OpenAI, Ollama (/v1), LM Studio, llama.cpp server, vLLM, etc.
"""

import json
import os
from typing import Iterator

import httpx

from .base import Provider, output_limit_error
from ..config import DEFAULT_MAX_TOKENS, ProviderConfig

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.model = cfg.model
        self.base_url = (cfg.base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY") or "sk-no-key"
        self.effort = cfg.reasoning_effort
        self.max_tokens = cfg.max_tokens or DEFAULT_MAX_TOKENS
        self.name = cfg.name or "openai"

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "stream": True,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if self.effort:   # local servers may reject it, so only when configured
            payload["reasoning_effort"] = self.effort
        # OpenAI's own API takes max_completion_tokens for reasoning models (and
        # rejects max_tokens); every other server takes max_tokens. Either way
        # it caps reasoning too, so a model can't think indefinitely.
        limit_key = "max_completion_tokens" if "api.openai.com" in self.base_url else "max_tokens"
        payload[limit_key] = self.max_tokens

        self.reasoning = ""
        answered = False
        finish_reason = None

        with httpx.Client(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    line = line.strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    try:
                        choice = json.loads(line)["choices"][0]
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    # llama.cpp, vLLM and LM Studio stream reasoning as
                    # reasoning_content; Ollama and OpenRouter as reasoning.
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if isinstance(thought, str):
                        self.reasoning += thought
                    content = delta.get("content")
                    if content:
                        answered = True
                        yield content
        if finish_reason == "length" and not answered:
            raise output_limit_error(self.name, self.max_tokens, bool(self.reasoning))
