"""
Subscription CLI providers: run the user's own installed `claude` / `codex` binary.

Seer never reads or handles the CLIs' credentials — it only launches the
official, unmodified binary, which authenticates with the user's own login.
The CLIs run with tools disabled / a read-only sandbox, from a neutral working
directory so project files (CLAUDE.md, AGENTS.md) are not picked up.
"""

import json
import re
import subprocess
import tempfile
from typing import Iterator

from .base import Provider
from ..config import CLI_COMMANDS, ProviderConfig, _cache_dir, resolve_cli_command

# Errors that mean "this model can't be used here", as opposed to auth, usage
# limits or network failures — only these trigger a fallback to the next model.
_MODEL_UNAVAILABLE = re.compile(
    r"model.*(not supported|not found|not available|unavailable|does not exist|invalid|unknown)"
    r"|(unknown|invalid|unsupported) model",
    re.IGNORECASE,
)


class ModelUnavailableError(RuntimeError):
    pass


class _CLIProvider(Provider):
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        # None = don't pass --model, i.e. use the CLI's own default.
        self.models = [
            None if not m or m == "auto" else m
            for m in [cfg.model, *cfg.fallback_models]
        ]
        path = resolve_cli_command(cfg.type, cfg.command)
        if not path:
            name = cfg.command or CLI_COMMANDS[cfg.type]
            raise ValueError(f"'{name}' CLI not found on PATH.")
        self.path = path

    def _parse(self, event: dict) -> Iterator[str]:
        raise NotImplementedError

    def _fail(self, message: str) -> None:
        name = self.cfg.name or self.cfg.type
        if _MODEL_UNAVAILABLE.search(message):
            raise ModelUnavailableError(f"{name}: {message}")
        raise RuntimeError(f"{name}: {message}")

    def _run_with_fallback(self, make_argv, stdin_text: str) -> Iterator[str]:
        """Try each configured model in order until one is available."""
        for i, model in enumerate(self.models):
            try:
                yield from self._run(make_argv(model), stdin_text)
                return
            except ModelUnavailableError:
                # Nothing is yielded before a model error, so retrying is safe.
                if i == len(self.models) - 1:
                    raise

    def _run(self, argv: list[str], stdin_text: str) -> Iterator[str]:
        workdir = _cache_dir()
        workdir.mkdir(parents=True, exist_ok=True)
        # stderr goes to a file so a chatty CLI can't fill the pipe and block.
        errfile = tempfile.TemporaryFile(mode="w+")
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errfile,
            text=True,
            cwd=workdir,
        )
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                yield from self._parse(event)
            proc.wait()
            if proc.returncode != 0:
                errfile.seek(0)
                stderr = errfile.read().strip()
                self._fail(stderr.splitlines()[-1] if stderr else f"exit code {proc.returncode}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            errfile.close()


class ClaudeCLIProvider(_CLIProvider):
    def _argv(self, system: str, model) -> list[str]:
        argv = [
            self.path, "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--tools", "",
            "--no-session-persistence",
            "--system-prompt", system,
        ]
        if model:
            argv += ["--model", model]
        return argv

    def _parse(self, event: dict) -> Iterator[str]:
        if event.get("type") == "stream_event":
            inner = event.get("event", {})
            delta = inner.get("delta", {})
            if inner.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                yield delta.get("text", "")
        elif event.get("type") == "result" and event.get("is_error"):
            self._fail(event.get("result") or "request failed")

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        return self._run_with_fallback(lambda model: self._argv(system, model), prompt)


class CodexCLIProvider(_CLIProvider):
    def _argv(self, model) -> list[str]:
        argv = [
            self.path, "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
        ]
        if model:
            argv += ["--model", model]
        if self.cfg.reasoning_effort:
            argv += ["-c", f"model_reasoning_effort={self.cfg.reasoning_effort}"]
        return argv + ["-"]

    def _parse(self, event: dict) -> Iterator[str]:
        etype = event.get("type")
        item = event.get("item", {})
        if etype == "item.completed" and item.get("type") == "agent_message":
            yield item.get("text", "")
        elif etype == "turn.failed":
            # Standalone "error" events can be transient (reconnects); the turn
            # failing is what's fatal.
            err = event.get("error") or {}
            msg = err.get("message") if isinstance(err, dict) else None
            self._fail(_unwrap_api_error(msg or "request failed"))

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        # codex exec has no system-prompt flag; prepend it to the prompt.
        return self._run_with_fallback(self._argv, f"{system}\n\n---\n\n{prompt}")


def _unwrap_api_error(message: str) -> str:
    """Codex embeds the raw API error JSON in its message; extract the readable part."""
    try:
        data = json.loads(message)
        return data.get("error", {}).get("message") or message
    except (json.JSONDecodeError, AttributeError):
        return message
