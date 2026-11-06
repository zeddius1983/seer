"""
`seer --tui`: tmux sessions on a private server, where seer is brave by default,
all panes share a memory and the status bar shows what seer is working with.

The server is separate (`tmux -L seer`) so its settings never touch the user's
own tmux. A session's shells get SEER_TUI from the server's environment, and
every seer run started in one finds it through that, so nothing needs to stay
running between questions. A run reports to the status bar when it has
something new to say, rather than tmux polling a Python command.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional, Sequence

from . import conversation
from .config import in_tui

SOCKET = "seer"
MIN_VERSION = (3, 2)   # for -T on the client and display-message -d
CONF = Path(__file__).parent / "shell" / "seer.tmux.conf"
STATUS_SCRIPT = Path(__file__).parent / "shell" / "seer-status.sh"

# gruvbox_dark, as in the shell prompt seer's status bar is styled after.
_FG0 = "#fbf1c7"
_INK = "#282828"
_BG1 = "#3c3836"
_BG3 = "#665c54"
_BLUE = "#458588"
_GREEN = "#98971a"
_ORANGE = "#d65d0e"
_PURPLE = "#b16286"
_RED = "#cc241d"
_YELLOW = "#d79921"

# Written as escapes so the private-use codepoints stay legible in any editor.
_GLYPHS = {
    "nerdfont": {
        "cap_left": "", "cap_right": "", "sep": "",
        "context": " ", "brave": " ", "dir": " ", "branch": " ",
    },
    "plain": {
        "cap_left": "", "cap_right": "", "sep": "",
        "context": "ctx ", "brave": "brave:", "dir": "", "branch": "",
    },
}

_OS_ICONS = {
    "arch": "", "manjaro": "", "ubuntu": "", "linuxmint": "",
    "debian": "", "raspbian": "", "fedora": "", "rhel": "",
    "centos": "", "rocky": "", "almalinux": "", "opensuse": "",
    "suse": "", "gentoo": "", "nixos": "", "alpine": "",
}
_APPLE_ICON = ""
_TUX_ICON = ""


def _os_icon() -> str:
    """The Nerd Font logo of this OS. Distrobox mounts the real host at /run/host."""
    if sys.platform == "darwin":
        return _APPLE_ICON
    for path in ("/run/host/etc/os-release", "/etc/os-release"):
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        fields = dict(re.findall(r'^(ID|ID_LIKE)="?([^"\n]*)"?$', text, re.MULTILINE))
        for name in [fields.get("ID", ""), *fields.get("ID_LIKE", "").split()]:
            if name in _OS_ICONS:
                return _OS_ICONS[name]
        break
    return _TUX_ICON


def _escape(text: str) -> str:
    """Make text safe to put in a tmux format, where # and % mean something."""
    return text.replace("#", "##").replace("%", "%%")


def _segments(parts: Sequence[tuple], glyphs: dict, joined: bool = False) -> str:
    """Draw (background, text) parts as one powerline group, in tmux styles.

    joined leaves the group open on the right, for what's drawn next to it.
    """
    out = ""
    previous = None
    for background, text in parts:
        ink = _INK if background == _YELLOW else _FG0
        if previous:
            out += f"#[fg={previous},bg={background}]{glyphs['sep']}"
        else:
            out += f"#[fg={background},bg=default]{glyphs['cap_left']}"
        out += f"#[fg={ink},bg={background}] {text} "
        previous = background
    if joined:
        return out
    return out + f"#[fg={previous},bg=default]{glyphs['cap_right']}#[default]"


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _usage_color(percent: int) -> str:
    if percent >= 80:
        return _RED
    return _YELLOW if percent >= 50 else _GREEN


def _context_part(used: Optional[int], window: Optional[int], glyphs: dict) -> tuple:
    """The context segment. `~`: used is estimated, not reported by the provider."""
    if used is None:
        size = _tokens(window) if window else "–"
        return _BG3, f"{glyphs['context']}{size}"
    if not window:
        return _BG3, f"{glyphs['context']}~{_tokens(used)}"
    percent = round(used * 100 / window)
    return _usage_color(percent), f"{glyphs['context']}~{_tokens(used)}/{_tokens(window)} {percent}%"


def status_right(label: str, used: Optional[int], window: Optional[int], confirm: str, style: str) -> str:
    glyphs = _GLYPHS[style]
    return _segments([
        (_PURPLE, _escape(label)),
        _context_part(used, window, glyphs),
        (_BLUE, f"{glyphs['brave']}{confirm}"),
        (_BG1, "%H:%M"),
    ], glyphs)


def status_left(style: str) -> str:
    """The session name, then the directory and git state of the active pane,
    which only a command can work out: see shell/seer-status.sh."""
    glyphs = _GLYPHS[style]
    icon = f"{_os_icon()} " if style == "nerdfont" else ""
    args = " ".join(shlex.quote(a) for a in (
        glyphs["sep"], glyphs["cap_right"], glyphs["dir"], glyphs["branch"],
    ))
    script = f"sh {shlex.quote(str(STATUS_SCRIPT))} #{{q:pane_current_path}} {args}"
    return _segments([(_ORANGE, f"{icon}#S")], glyphs, joined=True) + f"#({script})"


def push_status(label: str, used: Optional[int], window: Optional[int], confirm: str, style: str) -> None:
    """Show this run's model and context in the status bar of its session.
    Never breaks seer itself."""
    args = ["tmux", "set-option"]
    if os.environ.get("TMUX_PANE"):
        args += ["-t", os.environ["TMUX_PANE"]]
    try:
        subprocess.run(
            [*args, "status-right", status_right(label, used, window, confirm, style)],
            capture_output=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


class MeteredProvider:
    """Wraps a provider to report the size of each model call to the status bar.

    The reply counts too: it takes up the model's context window just as the
    request does. Providers don't report usage yet, so this is characters ÷ 4.
    """

    def __init__(self, provider, report):
        self._provider = provider
        self._report = report   # report(tokens)

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        chars = len(system) + len(prompt)
        try:
            for chunk in self._provider.stream(system, prompt):
                chars += len(chunk)
                yield chunk
        finally:
            self._report(chars // 4)


def session_name(words: Sequence[str]) -> str:
    """A session named by the user, or else after this directory. tmux doesn't
    allow . or : in names."""
    name = "-".join(words) or Path.cwd().name or "seer"
    return re.sub(r"[^\w-]+", "-", name).strip("-") or "seer"


def tmux_problem() -> Optional[str]:
    """What stops --tui from starting on this machine, if anything."""
    if not shutil.which("tmux"):
        return "seer --tui needs tmux, which isn't installed. Install it with your package manager (e.g. `brew install tmux`, `apt install tmux`, `pacman -S tmux`)."
    out = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    if m and (int(m.group(1)), int(m.group(2))) < MIN_VERSION:
        return f"seer --tui needs tmux {MIN_VERSION[0]}.{MIN_VERSION[1]} or newer (found: {out.strip()})."
    return None


def _ctrl_g_hint() -> Optional[str]:
    """A note if the shells in the session won't have Ctrl+G."""
    shell = Path(os.environ.get("SHELL", "")).name
    if shell != "zsh":
        return "Ctrl+G is only set up for zsh. Ask with: seer <question>"
    zshrc = Path(os.environ.get("ZDOTDIR") or Path.home()) / ".zshrc"
    try:
        if "seer-toolbox: seer-implicit" in zshrc.read_text(encoding="utf-8", errors="replace"):
            return None
    except OSError:
        pass
    return "Ctrl+G isn't set up: run seer's installer. Until then, ask with: seer <question>"


def _tmux(*args: str) -> list[str]:
    return ["tmux", "-L", SOCKET, "-u", "-f", str(CONF), *args]


def _env() -> dict:
    # Unset, or tmux refuses to start a client inside another one.
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def _closed_hook() -> str:
    """Delete a session's memory with it. `seer` may be gone by then, so no
    seer here: the server's pid and the session's id are what name the log."""
    # Joined by hand: a Path would collapse the // in the tmux format.
    log = f"{conversation.CONVERSATION_DIR}/" + conversation.session_log_name("#{pid}", "#{s/[$]//:hook_session}")
    return f"run-shell {shlex.quote('rm -f ' + log)}"


def create_session(name: str, label: str, window: Optional[int], confirm: str, style: str) -> None:
    """Create a session, unless there already is one by that name."""
    env = _env()
    exists = subprocess.run(_tmux("has-session", "-t", f"={name}"), env=env, capture_output=True)
    if exists.returncode == 0:
        return
    subprocess.run(_tmux(
        "new-session", "-d", "-s", name, "-c", os.getcwd(), ";",
        "set-option", "-t", f"={name}:", "status-right", status_right(label, None, window, confirm, style), ";",
        "set-option", "-g", "status-left", status_left(style), ";",
        "set-hook", "-g", "session-closed", _closed_hook(),
    ), env=env, check=True, capture_output=True, text=True)


def attach_session(name: str) -> int:
    """Join a session, or move to it from another one. Returns tmux's exit code."""
    colors = os.environ.get("COLORTERM") in ("truecolor", "24bit")
    join = (
        ["switch-client", "-t", f"={name}"] if in_tui() and _on_our_server()
        else ["attach-session", "-t", f"={name}"]
    )
    client = ["tmux", "-L", SOCKET, "-u", *(["-T", "RGB"] if colors else [])]
    hint = _ctrl_g_hint()
    notice = [";", "display-message", "-d", "8000", hint] if hint else []
    return subprocess.run([*client, *join, *notice], env=_env()).returncode


def _on_our_server() -> bool:
    return Path(os.environ.get("TMUX", "").split(",")[0]).name == SOCKET
