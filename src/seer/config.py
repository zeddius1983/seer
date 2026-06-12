import os
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "seer" / "config.yaml"

def _cache_dir() -> Path:
    if os.environ.get("XDG_CACHE_HOME"):
        return Path(os.environ["XDG_CACHE_HOME"]) / "seer"
    if os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Caches" / "seer"
    return Path.home() / ".cache" / "seer"

CONTEXT_FILE = _cache_dir() / "context"

DEFAULT_CONFIG = {
    "provider": "auto",
    "context_lines": 100,
    "providers": {
        "llamacpp": {
            "type": "openai",
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "llamacpp",
            "model": "auto",
        },
        "lmstudio": {
            "type": "openai",
            "base_url": "http://127.0.0.1:1234/v1",
            "api_key": "lmstudio",
            "model": "auto",
        },
        "ollama": {
            "type": "openai",
            "base_url": "http://localhost:11434/v1",
            "api_key": "ollama",
            "model": "auto",
        },
        "openai": {
            "type": "openai",
            "model": "gpt-4o",
            # api_key: set via OPENAI_API_KEY env var or here
        },
        "anthropic": {
            "type": "anthropic",
            "model": "claude-sonnet-4-6",
            # api_key: set via ANTHROPIC_API_KEY env var or here
        },
    },
}


def _list_openai_models(base_url: str, api_key: str) -> list[str]:
    """Probe an OpenAI-compatible /v1/models endpoint. Returns model ID list or [] on failure."""
    try:
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = httpx.get(url, headers=headers, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            return [m["id"] for m in data.get("data", []) if "id" in m]
    except Exception:
        pass
    return []

SYSTEM_PROMPT = """You are seer, a concise shell assistant embedded in the user's terminal.

## Formatting rules (always follow these)
- Always respond in well-structured Markdown.
- Use `inline code` for command names, flags, paths, and values.
- Use fenced code blocks with language tags for all commands and code:
  ```bash
  your command here
  ```
- Use **bold** for the most important action or fix.
- Use bullet lists for multiple steps or options.
- Keep responses short — no padding, no filler sentences.

## Behaviour
When given terminal context (error analysis mode):
- Look for errors, non-zero exit codes, or unexpected output.
- Lead with the **fix**, then a one-line explanation.
- If there is no error, say so in one sentence and stop.
- Ignore file contents printed by commands like `cat` — focus on command results only.

When asked a question (no context):
- Answer directly and concisely using the formatting rules above."""

DO_SYSTEM_PROMPT = """You are seer, a shell command assistant. The user wants you to perform a task on their system.

Your response must follow this exact structure:
1. One or two sentences explaining what the command will do.
2. Exactly one ```bash code block containing the complete command to execute.
3. Nothing after the code block.

Rules:
- Use a single command, pipe chain, or steps joined with && — keep it one block.
- Prefer safe, non-destructive commands. Avoid `sudo` unless the task requires it.
- Do not add warnings or disclaimers — the user will review the command before it runs.
- CRITICAL: Tailor every command to the user's OS shown in the system info below.
  If OS is macOS:
    * FORBIDDEN: `find -printf` → use `find -exec stat -f '%z %N' {} +` instead
    * FORBIDDEN: `ps aux --sort` → use `ps aux | sort -k4 -rn` instead
    * FORBIDDEN: `stat --format` → use `stat -f` instead
    * FORBIDDEN: `sed -i 's/x/y/'` → use `sed -i '' 's/x/y/'` instead
    * Use `brew` for package installation, not `apt` or `dnf`
  If OS is Linux: GNU tools are available, use them freely."""


@dataclass
class ProviderConfig:
    type: str          # "openai" | "anthropic"
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    name: Optional[str] = None        # resolved provider key (set when auto-resolved)
    model_was_auto: bool = False      # True when model was resolved from "auto"


@dataclass
class Config:
    provider: str
    providers: dict
    context_lines: int = 100
    system_prompt: str = SYSTEM_PROMPT

    def get_active_provider(self) -> ProviderConfig:
        provider_name = self.provider
        prefetched_models: list[str] = []

        if provider_name == "auto":
            provider_name, prefetched_models = self._resolve_auto_provider()
            if provider_name is None:
                raise ValueError(
                    "provider: auto — no configured provider is reachable.\n"
                    "Start a local LLM server (llama.cpp, LM Studio, Ollama) "
                    "or set a specific provider in your config."
                )

        if provider_name not in self.providers:
            raise ValueError(
                f"Provider '{provider_name}' not found in config. "
                f"Available: {list(self.providers.keys())}"
            )

        raw = self.providers[provider_name]
        model = raw.get("model", "")
        model_was_auto = model == "auto"

        if model_was_auto:
            models = prefetched_models or _list_openai_models(
                raw.get("base_url", ""), raw.get("api_key", "no-key")
            )
            if not models:
                raise ValueError(
                    f"model: auto — could not fetch model list from '{provider_name}'."
                )
            model = models[0]

        return ProviderConfig(
            type=raw.get("type", "openai"),
            model=model,
            api_key=raw.get("api_key"),
            base_url=raw.get("base_url"),
            name=provider_name,
            model_was_auto=model_was_auto,
        )

    def _resolve_auto_provider(self) -> tuple[Optional[str], list[str]]:
        """Return (provider_name, model_list) for the first reachable provider."""
        for name, raw in self.providers.items():
            if raw.get("type") != "openai" or not raw.get("base_url"):
                continue
            models = _list_openai_models(raw["base_url"], raw.get("api_key", "no-key"))
            if models:
                return name, models
        return None, []


def load_config() -> Config:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    # Deep merge with defaults
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if k != "providers"})

    providers = dict(DEFAULT_CONFIG["providers"])
    providers.update(data.get("providers", {}))
    merged["providers"] = providers

    return Config(
        provider=merged["provider"],
        providers=merged["providers"],
        context_lines=merged.get("context_lines", 100),
        system_prompt=merged.get("system_prompt", SYSTEM_PROMPT),
    )


def save_default_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
    return CONFIG_PATH
