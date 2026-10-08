"""
OpenAI-compatible provider.
Works with: OpenAI, Ollama (/v1), LM Studio, llama.cpp server, vLLM, etc.
"""

import json
import os
import re
from typing import Iterator

import httpx

from .base import Provider, output_limit_error
from ..config import DEFAULT_MAX_TOKENS, ProviderConfig

OPENAI_BASE_URL = "https://api.openai.com/v1"


# A server rejecting the output cap itself: vLLM, when prompt + max_tokens
# exceeds the model's context window ("This model's maximum context length is
# 8192 tokens…", "'max_tokens' … is too large").
_CAP_REJECTED = re.compile(r"max_(?:completion_)?tokens|maximum context length|context length", re.IGNORECASE)


class _CapRejected(Exception):
    pass


class OpenAIProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.model = cfg.model
        self.base_url = (cfg.base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY") or "sk-no-key"
        self.effort = cfg.reasoning_effort
        self.max_tokens = cfg.max_tokens or DEFAULT_MAX_TOKENS
        # The default cap is dropped if the server rejects it (a small context
        # window); one set in the config is the user's call and always sent.
        self.cap_configured = cfg.max_tokens is not None
        self.send_cap = True
        self.name = cfg.name or "openai"

    def stream(self, system: str, prompt: str) -> Iterator[str]:
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
        if self.send_cap:
            payload[limit_key] = self.max_tokens

        self.reasoning = ""
        try:
            yield from self._stream(payload, limit_key)
        except _CapRejected:
            # Nothing was yielded yet: retry once without the cap, and don't
            # send it again for this provider (brave mode calls it repeatedly).
            self.send_cap = False
            del payload[limit_key]
            yield from self._stream(payload, limit_key)

    def _stream(self, payload: dict, limit_key: str) -> Iterator[str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        answered = False
        finish_reason = None
        with httpx.Client(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code == 400 and limit_key in payload and not self.cap_configured:
                    if _CAP_REJECTED.search(resp.read().decode("utf-8", errors="replace")):
                        raise _CapRejected()
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
        if finish_reason == "length" and not answered and limit_key in payload:
            raise output_limit_error(self.name, self.max_tokens, bool(self.reasoning))
