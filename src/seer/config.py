import os
import shutil
import yaml
from dataclasses import dataclass, field
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

CLI_COMMANDS = {"claude-cli": "claude", "codex-cli": "codex"}


def resolve_cli_command(provider_type: str, command: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of a subscription CLI binary, or None if not installed."""
    return shutil.which(command or CLI_COMMANDS.get(provider_type, ""))


DEFAULT_CONFIG = {
    "provider": "auto",
    "context_lines": 100,
    "stream": False,
    # Brave mode: seer runs the commands needed to answer free-form queries
    # (incl. Ctrl+G) itself. Anything not known to be read-only asks first.
    "brave": False,
    # When brave mode asks before running a command:
    #   auto   — unless it's known read-only (default)
    #   trust  — only for known-destructive commands (rm, sudo, > file, git push, …)
    #   always — every command
    # A command the model itself flags as changing something always asks.
    "brave_confirm": "auto",
    # Opt-in: let `provider: auto` fall back to your own Claude Code / Codex CLI
    # subscription when no local server is running. false | true | [ordered list]
    "auto_cli": False,
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
        "vllm": {
            "type": "openai",
            "base_url": "http://localhost:8000/v1",
            "api_key": "vllm",
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
        # Subscription CLIs: run your installed `claude` / `codex` with your own login.
        # Never used unless selected explicitly or listed in auto_cli.
        "claude-cli": {
            "type": "claude-cli",
            "model": "sonnet",          # alias (haiku / sonnet / opus) or full model id
        },
        "codex-cli": {
            "type": "codex-cli",
            # Tried in order; falls back to the next one if a model is unavailable.
            # auto = the model set in ~/.codex/config.toml
            "model": ["gpt-6-luna", "auto"],
            "reasoning_effort": "low",
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

_OS_RULES = """- CRITICAL: Tailor every command to the user's OS shown in the system info below.
  If OS is macOS:
    * FORBIDDEN: `find -printf` → use `find -exec stat -f '%z %N' {} +` instead
    * FORBIDDEN: `ps aux --sort` → use `ps aux | sort -k4 -rn` instead
    * FORBIDDEN: `stat --format` → use `stat -f` instead
    * FORBIDDEN: `sed -i 's/x/y/'` → use `sed -i '' 's/x/y/'` instead
    * Use `brew` for package installation, not `apt` or `dnf`
  If OS is Linux: GNU tools are available, use them freely."""

DO_SYSTEM_PROMPT = """You are seer, a shell command assistant. The user wants you to perform a task on their system.

Your response must follow this exact structure:
1. One or two sentences explaining what the command will do.
2. Exactly one ```bash code block containing the complete command to execute.
3. Nothing after the code block.

Rules:
- Use a single command, pipe chain, or steps joined with && — keep it one block.
- Prefer safe, non-destructive commands. Avoid `sudo` unless the task requires it.
- Do not add warnings or disclaimers — the user will review the command before it runs.
""" + _OS_RULES

BRAVE_SYSTEM_PROMPT = """You are seer in brave mode: you complete the user's task by running shell commands on their machine yourself, then answer with the result.

## Protocol
Each reply is EITHER one command to run OR the final answer — never both.

To run a command, reply with exactly one fenced block and nothing else. Tag it `run` if the command only reads:
```run
du -ah . | sort -rh | head -5
```
Tag it `run-write` if it creates, changes or deletes anything — files, processes, settings, packages or git state. Tag by effect, not by tool: a `python3 -c` script that only reads and prints is `run`.
```run-write
gzip old.log
```
You then receive its output and exit code, and may run another command or answer.

When you have enough information, reply with the final answer and no `run` block:
- Markdown, as short as the result allows. Use a table only for several items with attributes (files with sizes, processes with memory).
- Base it only on the command output you received — never invent results.
- If the task changed something, confirm in one sentence what was done (e.g. "Created `notes.txt` containing `hello`.").
- Lead with the result. No title or heading (no "Final Answer"), and don't describe the commands you ran unless it matters.

## Rules
- Commands run non-interactively: no stdin, no TTY, ~60s timeout. Never use pagers, editors, `sudo`, `top`, `watch` or `tail -f`.
- Commands run in the user's working directory (shown below); "here" means that directory.
- Prefer read-only commands. Combine steps with pipes or && — most tasks need 1–2 commands.
- Keep output small (e.g. `head`) — long output is truncated.
- `run-write` commands are shown to the user for approval first — never tag a command `run` to avoid that. If the user declines, stop and answer without running anything else.
""" + _OS_RULES


@dataclass
class ProviderConfig:
    type: str          # "openai" | "anthropic"
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    name: Optional[str] = None        # resolved provider key (set when auto-resolved)
    model_was_auto: bool = False      # True when model was resolved from "auto"
    command: Optional[str] = None     # CLI providers: binary name/path override
    reasoning_effort: Optional[str] = None  # codex-cli only
    fallback_models: list[str] = field(default_factory=list)  # CLI providers: tried if `model` is unavailable


@dataclass
class Config:
    provider: str
    providers: dict
    context_lines: int = 100
    system_prompt: str = SYSTEM_PROMPT
    stream: bool = False
    brave: bool = False
    brave_confirm: str = "auto"       # auto | trust | always — see brave.CONFIRM_POLICIES
    auto_cli: object = False          # False | True | list of CLI provider names

    def _auto_cli_order(self) -> list[str]:
        if self.auto_cli is True:
            return list(CLI_COMMANDS)
        if isinstance(self.auto_cli, str):
            return [self.auto_cli]
        if isinstance(self.auto_cli, list):
            return [str(n) for n in self.auto_cli]
        return []

    def get_active_provider(self) -> ProviderConfig:
        provider_name = self.provider
        prefetched_models: list[str] = []

        if provider_name == "auto":
            provider_name, prefetched_models = self._resolve_auto_provider()
            if provider_name is None:
                raise ValueError(
                    "provider: auto — no configured provider is reachable.\n"
                    "Start a local LLM server (llama.cpp, LM Studio, Ollama), "
                    "set a specific provider in your config, or opt in to your "
                    "Claude Code / Codex subscription with `auto_cli: true`."
                )

        if provider_name not in self.providers:
            raise ValueError(
                f"Provider '{provider_name}' not found in config. "
                f"Available: {list(self.providers.keys())}"
            )

        raw = self.providers[provider_name]
        ptype = raw.get("type", "openai")
        model = raw.get("model", "")
        fallback_models: list[str] = []
        # CLI providers accept an ordered list of models to fall back through.
        if isinstance(model, list) and ptype in CLI_COMMANDS:
            model, *fallback_models = [str(m) for m in model] or ["auto"]
        model_was_auto = model == "auto"

        # CLI providers: "auto" means "let the CLI use its own default model".
        if model_was_auto and ptype not in CLI_COMMANDS:
            models = prefetched_models or _list_openai_models(
                raw.get("base_url", ""), raw.get("api_key", "no-key")
            )
            if not models:
                raise ValueError(
                    f"model: auto — could not fetch model list from '{provider_name}'."
                )
            model = models[0]

        return ProviderConfig(
            type=ptype,
            model=model,
            api_key=raw.get("api_key"),
            base_url=raw.get("base_url"),
            name=provider_name,
            model_was_auto=model_was_auto,
            command=raw.get("command"),
            reasoning_effort=raw.get("reasoning_effort"),
            fallback_models=fallback_models,
        )

    def _resolve_auto_provider(self) -> tuple[Optional[str], list[str]]:
        """Return (provider_name, model_list) for the first reachable provider."""
        for name, raw in self.providers.items():
            if raw.get("type") != "openai" or not raw.get("base_url"):
                continue
            models = _list_openai_models(raw["base_url"], raw.get("api_key", "no-key"))
            if models:
                return name, models
        # Local servers first; subscription CLIs only if the user opted in.
        for name in self._auto_cli_order():
            raw = self.providers.get(name)
            if not raw or raw.get("type") not in CLI_COMMANDS:
                continue
            if resolve_cli_command(raw["type"], raw.get("command")):
                return name, []
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
        stream=bool(merged.get("stream", False)),
        brave=bool(merged.get("brave", False)),
        brave_confirm=_brave_confirm(merged.get("brave_confirm", "auto")),
        auto_cli=merged.get("auto_cli", False),
    )


def _brave_confirm(value) -> str:
    value = str(value).lower()
    if value not in ("auto", "trust", "always"):
        raise ValueError(f"brave_confirm must be auto, trust or always (got '{value}').")
    return value


def save_default_config():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)
    return CONFIG_PATH
