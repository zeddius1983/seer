"""Tests for CLI response rendering."""

from unittest.mock import patch

import pytest
from rich.console import Console
from rich.text import Text

from seer.cli import _TailRenderable, _command_panel, _format_command, _split_at_operators, stream_response


class _Config:
    def get_active_provider(self):
        return object()


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
