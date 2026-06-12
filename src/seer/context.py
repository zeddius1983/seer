"""
Terminal context capture for seer.

Priority order:
  1. Piped stdin (most explicit)
  2. tmux capture-pane (always current — beats a potentially stale hook file)
  3. Saved context file (written by shell hooks, used when not in tmux)
  4. Shell history fallback (last N history entries)
"""

import os
import select
import stat as stat_module
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator, Optional, Tuple

from .config import CONTEXT_FILE


def get_context(lines: int = 100) -> Optional[str]:
    """Return the best available terminal context string, or None."""
    # 1. Piped stdin — only consume if data is actually available
    if not sys.stdin.isatty() and _stdin_has_data():
        data, _ = _read_stdin_until_idle()
        return data.strip() or None

    # 2. tmux capture — always reflects the current live pane output
    tmux_ctx = _tmux_capture(lines)
    if tmux_ctx:
        return tmux_ctx

    # 3. Saved context file from shell hook (used when not in tmux)
    if CONTEXT_FILE.exists():
        text = CONTEXT_FILE.read_text().strip()
        if text:
            return _last_n_lines(text, lines)

    # 4. Shell history fallback
    return _history_fallback(10)


def _read_stdin_until_idle(idle_timeout: float = 1.0) -> Tuple[str, bool]:
    """Read stdin until no new data arrives for idle_timeout seconds.

    Returns (data, got_eof). got_eof=False means the pipe is still open
    (e.g. tail -f) — the caller should treat this as a streaming source.
    """
    fd = sys.stdin.fileno()
    chunks = []
    got_eof = False
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], idle_timeout)
        if not ready:
            break
        chunk = os.read(fd, 65536)
        if not chunk:  # EOF
            got_eof = True
            break
        chunks.append(chunk.decode("utf-8", errors="replace"))
    return "".join(chunks), got_eof


def read_stdin_batches(interval: float = 15.0) -> Generator[str, None, None]:
    """Yield batches of stdin lines on a fixed time window.

    Collects for `interval` seconds then yields whatever arrived.
    Empty windows are skipped. Stops on EOF (pipe closed).
    Intended for infinite streams like `tail -f`.
    """
    fd = sys.stdin.fileno()
    while True:
        chunks = []
        deadline = time.monotonic() + interval
        eof = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([sys.stdin], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    eof = True
                    break
                chunks.append(chunk.decode("utf-8", errors="replace"))
        data = "".join(chunks)
        if data.strip():
            yield data
        if eof:
            break


def _stdin_has_data() -> bool:
    """Return True if stdin is an actual pipe or file with data (not /dev/null or a pty)."""
    try:
        mode = os.fstat(sys.stdin.fileno()).st_mode
        # Only treat as piped input if stdin is a real pipe (FIFO) or regular file
        if not (stat_module.S_ISFIFO(mode) or stat_module.S_ISREG(mode)):
            return False
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (ValueError, OSError):
        return False


def _last_n_lines(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:])


def _tmux_capture(lines: int) -> Optional[str]:
    if not os.environ.get("TMUX"):
        return None
    try:
        args = ["tmux", "capture-pane", "-p", "-S", str(-lines)]
        if os.environ.get("TMUX_PANE"):
            args.extend(["-t", os.environ["TMUX_PANE"]])
            
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _history_fallback(n: int) -> Optional[str]:
    """Read last n entries from $HISTFILE, reading only the tail of the file."""
    histfile = os.environ.get("HISTFILE", "")
    if not histfile:
        shell = os.environ.get("SHELL", "")
        if "zsh" in shell:
            histfile = str(Path.home() / ".zsh_history")
        else:
            histfile = str(Path.home() / ".bash_history")

    path = Path(histfile)
    if not path.exists():
        return None

    try:
        # Read only the last 64 KB to avoid loading huge history files
        tail_bytes = 65536
        with open(path, "rb") as f:
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            read_pos = max(0, file_size - tail_bytes)
            f.seek(read_pos)
            raw = f.read().decode("utf-8", errors="replace")

        # If we didn't read from the start, discard the first (partial) line
        if read_pos > 0:
            first_nl = raw.find("\n")
            if first_nl != -1:
                raw = raw[first_nl + 1:]

        # zsh history may use extended format (; lines). Strip those.
        lines = []
        for line in raw.splitlines():
            if line.startswith(":") and line.count(":") >= 2:
                # extended format ": <timestamp>:<elapsed>;<cmd>"
                parts = line.split(";", 1)
                if len(parts) == 2:
                    lines.append("$ " + parts[1])
            elif line.strip():
                lines.append("$ " + line.strip())
        return "\n".join(lines[-n:]) or None
    except OSError:
        return None

