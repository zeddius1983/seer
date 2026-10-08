"""Tests for CLI response rendering."""

from unittest.mock import patch

import pytest
from rich.console import Console
from rich.text import Text

from seer.cli import _TailRenderable, _command_panel, _format_command, _split_at_operators, model_label, stream_response
from seer.config import ProviderConfig


class _Config:
    def get_active_provider(self):
        return ProviderConfig(type="anthropic", model="claude-sonnet-5-5")


class _RecordingLive:
    instances = []

    def __init__(self, renderable, **kwargs):
        self.renderable = renderable
        self.kwargs = kwargs
        self.updates = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def update(self, renderable):
        self.updates.append(renderable)


@pytest.fixture(autouse=True)
def _reset_recorded_live_instances():
    _RecordingLive.instances.clear()


def test_streaming_uses_transient_cropped_display_for_long_responses():
    provider = type("Provider", (), {"stream": lambda self, *_: iter(["response"])})()

    with (
        patch("seer.cli.get_provider", return_value=provider),
        patch("seer.cli.Live", _RecordingLive),
    ):
        stream_response("system", "prompt", _Config(), stream=True)

    live = _RecordingLive.instances[0]
    assert live.kwargs["transient"] is True
    assert live.kwargs["vertical_overflow"] == "crop"
    assert isinstance(live.updates[0], _TailRenderable)


def test_status_line_names_the_model():
    provider = type("Provider", (), {"stream": lambda self, *_: iter(["two words"])})()

    with (
        patch("seer.cli.get_provider", return_value=provider),
        patch("seer.cli.Live", _RecordingLive),
    ):
        stream_response("system", "prompt", _Config())

    live = _RecordingLive.instances[0]
    assert live.renderable.plain == "  Sonnet 5.5 thinking…"
    assert live.updates[-1].plain == "  Sonnet 5.5 thinking… (2 words)"


def test_model_label_prefers_the_resolved_model():
    pcfg = ProviderConfig(type="claude-cli", model="sonnet")
    provider = type("Provider", (), {"resolved_model": "claude-sonnet-5-5"})()
    assert model_label(pcfg, provider) == "Sonnet 5.5"


@pytest.mark.parametrize("ptype, model, label", [
    ("anthropic", "claude-sonnet-5-5", "Sonnet 5.5"),
    ("anthropic", "claude-opus-4-8", "Opus 4.8"),
    ("anthropic", "claude-haiku-4-5", "Haiku 4.5"),
    ("anthropic", "claude-sonnet-4-5-20250929", "Sonnet 4.5"),
    ("anthropic", "claude-sonnet-5", "Sonnet 5"),
    ("openai", "gpt-6.1-sol", "GPT-6.1 Sol"),
    ("openai", "gpt-4o", "GPT-4o"),
    ("openai", "gpt-oss:20b", "gpt-oss:20b"),
    ("openai", "google/gemma-3-12b", "gemma-3-12b"),
    ("claude-cli", "sonnet", "Sonnet"),
    ("claude-cli", "auto", "Claude Code"),
    ("codex-cli", "gpt-6-luna", "GPT-6 Luna"),
    ("codex-cli", "auto", "Codex"),
])
def test_model_label(ptype, model, label):
    assert model_label(ProviderConfig(type=ptype, model=model)) == label


def test_tail_renderable_keeps_the_latest_terminal_lines():
    output = Console(width=20, height=3).render_str("one\ntwo\nthree\nfour")
    rendered_lines = Console(width=20, height=3).render_lines(
        _TailRenderable(output), pad=False
    )
    rendered = "\n".join("".join(segment.text for segment in line) for line in rendered_lines)

    assert "one" not in rendered
    assert "two" not in rendered
    assert "three" in rendered
    assert "four" in rendered


@pytest.mark.parametrize("failure", [None, RuntimeError("provider failed")])
def test_streaming_clears_connecting_status_when_no_content_arrives(failure):
    class Provider:
        def stream(self, *_):
            if failure:
                raise failure
            return iter(())

    with (
        patch("seer.cli.get_provider", return_value=Provider()),
        patch("seer.cli.Live", _RecordingLive),
    ):
        if failure:
            with pytest.raises(RuntimeError, match="provider failed"):
                stream_response("system", "prompt", _Config(), stream=True)
        else:
            stream_response("system", "prompt", _Config(), stream=True)

    final_update = _RecordingLive.instances[0].updates[-1]
    assert isinstance(final_update, Text)
    assert final_update.plain == ""


def test_help_and_version_show_seer_version():
    from click.testing import CliRunner
    from seer import __version__
    from seer.cli import main

    runner = CliRunner()
    assert f"seer v{__version__}" in runner.invoke(main, ["--help"]).output
    for flag in ("--version", "-V"):
        assert runner.invoke(main, [flag]).output.strip() == f"seer, version {__version__}"


class TestFormatCommand:
    def test_short_command_unchanged(self):
        assert _format_command("ls | head", 80) == "ls | head"

    def test_long_command_breaks_before_operators(self):
        cmd = "cd /repo && git log -20 --name-only | sort | uniq -c || true; echo done"
        assert _format_command(cmd, 20) == (
            "cd /repo \\\n  && git log -20 --name-only \\\n  | sort \\\n  | uniq -c"
            " \\\n  || true \\\n  ; echo done"
        )

    def test_operators_in_quotes_and_substitutions_are_kept(self):
        cmd = "grep -E 'a|b' x && echo \"c;d\" $(ls | wc -l) 2>&1 | head"
        assert _split_at_operators(cmd) == [
            "grep -E 'a|b' x", "&& echo \"c;d\" $(ls | wc -l) 2>&1", "| head",
        ]

    def test_redirect_pipe_and_multiline_left_alone(self):
        assert _split_at_operators("ls >| out") == ["ls >| out"]
        assert _format_command("cat <<EOF\n" + "x" * 90 + "\nEOF", 20).startswith("cat <<EOF\n")

    def test_formatted_command_is_valid_shell(self):
        import subprocess
        cmd = "echo one && echo two | tr a-z A-Z; echo three"
        formatted = _format_command(cmd, 10)
        run = lambda c: subprocess.run(["bash", "-c", c], capture_output=True, text=True).stdout
        assert "\\\n" in formatted
        assert run(formatted) == run(cmd)

    def test_panel_shows_whole_long_command(self):
        cmd = "find . -name '*.py' -exec wc -l {} + | sort -rn | head -5 && echo " + "z" * 120
        console = Console(width=60, record=True)
        with patch("seer.cli.console", console):
            console.print(_command_panel(cmd))
        text = "".join(console.export_text().split())
        assert "z" * 120 in text.replace("│", "")
        assert "…" not in text


class TestBravePipedInput:
    @pytest.fixture
    def invoke(self, monkeypatch):
        from click.testing import CliRunner
        import seer.cli as cli

        calls = {"brave": [], "watch": [], "reattached": 0}
        monkeypatch.setattr(cli, "_stdin_is_piped", lambda: True)
        monkeypatch.setattr(cli, "reattach_tty", lambda: calls.__setitem__("reattached", calls["reattached"] + 1))
        monkeypatch.setattr(cli, "_cmd_brave", lambda task, cfg, raw, piped=None: calls["brave"].append((task, piped)))
        monkeypatch.setattr(cli, "_cmd_watch", lambda first, focus, *a: calls["watch"].append((first, focus)))

        def invoke(stdin, got_eof, *args):
            monkeypatch.setattr(cli, "_read_stdin_until_idle", lambda idle_timeout=1.0: (stdin, got_eof))
            result = CliRunner().invoke(cli.main, ["-b", *args, "explain", "commits"])
            assert result.exit_code == 0, result.output
            return calls

        return invoke

    def test_piped_input_is_passed_to_brave_mode(self, invoke):
        calls = invoke("abc123 fix bug\n", True)
        assert calls["brave"] == [("explain commits", "abc123 fix bug")]
        assert calls["reattached"] == 1

    def test_open_stream_goes_to_watch_mode(self, invoke):
        calls = invoke("line 1\n", False)
        assert calls["watch"] == [("line 1\n", "explain commits")]
        assert calls["brave"] == []

    def test_empty_pipe_runs_brave_without_input(self, invoke):
        calls = invoke("", False)
        assert calls["brave"] == [("explain commits", None)]

    def test_no_context_ignores_piped_input(self, invoke):
        calls = invoke("abc123 fix bug\n", True, "--no-context")
        assert calls["brave"] == [("explain commits", None)]
        assert calls["reattached"] == 1
