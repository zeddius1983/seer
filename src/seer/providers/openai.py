"""
OpenAI-compatible provider.
Works with: OpenAI, Ollama (/v1), LM Studio, llama.cpp server, vLLM, etc.
"""

import json
import os
from typing import Iterator

import httpx

from .base import Provider
from ..config import ProviderConfig

OPENAI_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.model = cfg.model
        self.base_url = (cfg.base_url or OPENAI_BASE_URL).rstrip("/")
        self.api_key = cfg.api_key or os.environ.get("OPENAI_API_KEY") or "sk-no-key"

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
                        chunk = json.loads(line)
                        delta = chunk["choices"][0]["delta"]
                        content = delta.get("content")
                        if content:
                            yield content
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
