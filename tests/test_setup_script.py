"""Tests for shell/setup.sh's ~/.zshrc handling, with stand-ins for uv and seer."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parent.parent / "shell" / "setup.sh"


@pytest.fixture
def home(tmp_path):
    """A throwaway home with fake `uv` and `seer` first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shell_file = tmp_path / "seer.zsh"
    shell_file.write_text("# integration\n")
    for name, body in {
        "uv": f'[ "$1 $2" = "tool dir" ] && echo "{tmp_path}/tools"; exit 0',
        "seer": f'[ "$1" = "--shell-path" ] && echo "{shell_file}"; exit 0',
    }.items():
        script = bindir / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    (tmp_path / ".zshrc").write_text("export EDITOR=vim\n")
    return tmp_path


def _setup(home, *args, bind=None):
    env = {
        "HOME": str(home),
        "ZDOTDIR": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": f"{home / 'bin'}{os.pathsep}/usr/bin:/bin",
    }
    if bind:
        env["SEER_IMPLICIT_BIND"] = bind
    subprocess.run(["bash", str(SETUP), *args], env=env, check=True, capture_output=True)
    return (home / ".zshrc").read_text()


def _blocks(zshrc):
    return [line for line in zshrc.splitlines() if line.startswith("# -- seer-toolbox: ")]


def test_install_adds_both_blocks(home):
    zshrc = _setup(home)
    assert _blocks(zshrc) == ["# -- seer-toolbox: seer --", "# -- seer-toolbox: seer-implicit --"]
    assert f'source "{home / "seer.zsh"}"' in zshrc
    assert "export EDITOR=vim" in zshrc


def test_reinstall_keeps_the_shell_integration(home):
    # The bug: "seer-toolbox: seer" also matched "seer-toolbox: seer-implicit",
    # so a re-run removed the integration and then skipped adding it back.
    _setup(home)
    zshrc = _setup(home)
    assert sorted(_blocks(zshrc)) == ["# -- seer-toolbox: seer --", "# -- seer-toolbox: seer-implicit --"]
    assert zshrc.count(f'source "{home / "seer.zsh"}"') == 1


def test_reinstall_restores_integration_lost_to_the_old_bug(home):
    # A ~/.zshrc left by an affected re-install: only the implicit block.
    _setup(home)
    zshrc = home / ".zshrc"
    text = zshrc.read_text()
    start = text.index("# -- seer-toolbox: seer --")
    end = text.index("# -- end seer-toolbox: seer --") + len("# -- end seer-toolbox: seer --\n")
    zshrc.write_text(text[:start] + text[end:])
    assert "seer-toolbox: seer --" not in zshrc.read_text()
    assert "# -- seer-toolbox: seer --" in _blocks(_setup(home))


def test_reinstall_can_change_the_key(home):
    _setup(home, bind="^@")
    zshrc = _setup(home, bind="^G")
    assert "bindkey '^G' _seer_implicit_mode" in zshrc
    assert "^@" not in zshrc


def test_uninstall_removes_both_blocks(home):
    _setup(home)
    zshrc = _setup(home, "--uninstall")
    assert _blocks(zshrc) == []
    assert "export EDITOR=vim" in zshrc
