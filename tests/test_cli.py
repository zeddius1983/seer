"""Tests for CLI response rendering."""

from unittest.mock import patch

import pytest
from rich.console import Console
from rich.text import Text

from seer.cli import _TailRenderable, stream_response


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
