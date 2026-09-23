"""Tests for the claude-cli / codex-cli subprocess providers, using fake binaries."""

import json
import stat

import pytest

from seer.config import ProviderConfig
from seer.providers import get_provider


def _fake_cli(tmp_path, name, events, exit_code=0, stderr=""):
    """Write an executable that records its argv/stdin and prints JSONL events."""
    lines = "\n".join(json.dumps(e) for e in events)
    script = tmp_path / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'stdin': sys.stdin.read()}}, open({str(tmp_path / 'call.json')!r}, 'w'))\n"
        f"print({lines!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _call(tmp_path):
    return json.loads((tmp_path / "call.json").read_text())


def _text_delta(text):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}


class TestClaudeCLI:
    def test_streams_text_deltas_and_passes_system_prompt(self, tmp_path):
        cmd = _fake_cli(tmp_path, "claude", [
            {"type": "system", "subtype": "init"},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "thinking_delta", "thinking": "hmm"}}},
            _text_delta("Hello"),
            _text_delta(" world"),
            {"type": "result", "is_error": False, "result": "Hello world"},
        ])
        provider = get_provider(ProviderConfig(type="claude-cli", model="haiku", command=cmd))

        assert "".join(provider.stream("SYS", "PROMPT")) == "Hello world"
        call = _call(tmp_path)
        assert call["stdin"] == "PROMPT"
        argv = call["argv"]
        assert argv[argv.index("--system-prompt") + 1] == "SYS"
        assert argv[argv.index("--model") + 1] == "haiku"
        assert argv[argv.index("--tools") + 1] == ""
        assert "--no-session-persistence" in argv

    def test_model_auto_omits_model_flag(self, tmp_path):
        cmd = _fake_cli(tmp_path, "claude", [_text_delta("ok")])
        provider = get_provider(ProviderConfig(type="claude-cli", model="auto", command=cmd))
        list(provider.stream("SYS", "PROMPT"))
        assert "--model" not in _call(tmp_path)["argv"]

    def test_error_result_raises(self, tmp_path):
        cmd = _fake_cli(tmp_path, "claude", [
            {"type": "result", "is_error": True, "result": "Not logged in"},
        ], exit_code=1)
        provider = get_provider(ProviderConfig(type="claude-cli", model="sonnet", command=cmd))
        with pytest.raises(RuntimeError, match="Not logged in"):
            list(provider.stream("SYS", "PROMPT"))


class TestCodexCLI:
    def test_yields_agent_message_with_system_prepended(self, tmp_path):
        cmd = _fake_cli(tmp_path, "codex", [
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Use `ls -la`."}},
            {"type": "turn.completed"},
        ])
        provider = get_provider(ProviderConfig(
            type="codex-cli", model="gpt-x", command=cmd, reasoning_effort="low"))

        assert "".join(provider.stream("SYS", "PROMPT")) == "Use `ls -la`."
        call = _call(tmp_path)
        assert call["stdin"].startswith("SYS")
        assert call["stdin"].endswith("PROMPT")
        argv = call["argv"]
        assert argv[0] == "exec"
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert argv[argv.index("--model") + 1] == "gpt-x"
        assert "model_reasoning_effort=low" in argv
        assert "--ephemeral" in argv

    def test_nonzero_exit_reports_stderr(self, tmp_path):
        cmd = _fake_cli(tmp_path, "codex", [], exit_code=2, stderr="noise\nnot authenticated")
        provider = get_provider(ProviderConfig(type="codex-cli", model="auto", command=cmd))
        with pytest.raises(RuntimeError, match="not authenticated"):
            list(provider.stream("SYS", "PROMPT"))

    def test_turn_failed_raises(self, tmp_path):
        cmd = _fake_cli(tmp_path, "codex", [
            {"type": "turn.failed", "error": {"message": "usage limit reached"}},
        ])
        provider = get_provider(ProviderConfig(type="codex-cli", model="auto", command=cmd))
        with pytest.raises(RuntimeError, match="usage limit reached"):
            list(provider.stream("SYS", "PROMPT"))


def test_missing_binary_raises():
    with pytest.raises(ValueError, match="not found on PATH"):
        get_provider(ProviderConfig(type="claude-cli", model="sonnet",
                                    command="/nonexistent/claude"))


def _fake_codex_rejecting(tmp_path, rejected_model, error="model is not supported when using Codex with a ChatGPT account."):
    """Fake codex that fails the turn for `rejected_model` and answers otherwise.
    Appends each invocation's --model (or None) to calls.jsonl."""
    script = tmp_path / "codex"
    api_error = json.dumps({"type": "error", "status": 400,
                            "error": {"type": "invalid_request_error", "message": error}})
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "argv = sys.argv[1:]; sys.stdin.read()\n"
        "model = argv[argv.index('--model') + 1] if '--model' in argv else None\n"
        f"open({str(tmp_path / 'calls.jsonl')!r}, 'a').write(json.dumps(model) + '\\n')\n"
        f"if model == {rejected_model!r}:\n"
        f"    print(json.dumps({{'type': 'turn.failed', 'error': {{'message': {api_error!r}}}}}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'answer'}}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _models_called(tmp_path):
    return [json.loads(l) for l in (tmp_path / "calls.jsonl").read_text().splitlines()]


class TestModelFallback:
    def test_uses_first_model_when_available(self, tmp_path):
        cmd = _fake_codex_rejecting(tmp_path, "something-else")
        provider = get_provider(ProviderConfig(
            type="codex-cli", model="gpt-6-luna", fallback_models=["auto"], command=cmd))
        assert "".join(provider.stream("SYS", "PROMPT")) == "answer"
        assert _models_called(tmp_path) == ["gpt-6-luna"]

    def test_falls_back_to_auto_when_model_unavailable(self, tmp_path):
        cmd = _fake_codex_rejecting(tmp_path, "gpt-6-luna")
        provider = get_provider(ProviderConfig(
            type="codex-cli", model="gpt-6-luna", fallback_models=["auto"], command=cmd))
        assert "".join(provider.stream("SYS", "PROMPT")) == "answer"
        assert _models_called(tmp_path) == ["gpt-6-luna", None]

    def test_raises_readable_error_when_no_fallback_left(self, tmp_path):
        cmd = _fake_codex_rejecting(tmp_path, "gpt-6-luna")
        provider = get_provider(ProviderConfig(type="codex-cli", model="gpt-6-luna", command=cmd))
        with pytest.raises(RuntimeError, match=r"^codex-cli: model is not supported"):
            list(provider.stream("SYS", "PROMPT"))

    def test_non_model_errors_do_not_fall_back(self, tmp_path):
        cmd = _fake_codex_rejecting(tmp_path, "gpt-6-luna", error="You've hit your usage limit.")
        provider = get_provider(ProviderConfig(
            type="codex-cli", model="gpt-6-luna", fallback_models=["auto"], command=cmd))
        with pytest.raises(RuntimeError, match="usage limit"):
            list(provider.stream("SYS", "PROMPT"))
        assert _models_called(tmp_path) == ["gpt-6-luna"]
