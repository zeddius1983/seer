"""
Machine-readable trace of one seer run, for the end-to-end scenarios in e2e/.

Off unless the SEER_TRACE environment variable names a file. Each event is
appended to it as one JSON line: the mode, the provider, every model call,
every command brave mode runs, confirmations, the answer and errors. An
environment variable rather than a flag keeps scenario commands exactly what
a user would type, pipes included.
"""

import atexit
import json
import os
import sys
import time
from typing import Iterator

_START = time.monotonic()


def enabled() -> bool:
    return bool(os.environ.get("SEER_TRACE"))


def event(kind: str, **fields) -> None:
    """Append one event to the trace file, if tracing is on."""
    path = os.environ.get("SEER_TRACE")
    if not path:
        return
    record = {"event": kind, "t": round(time.monotonic() - _START, 3), **fields}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass   # tracing must never break seer itself


def start(version: str) -> None:
    """The first event of a run; the last one, `end`, is written at exit."""
    if not enabled():
        return
    event("start", version=version, argv=sys.argv[1:], cwd=os.getcwd())
    atexit.register(lambda: event("end"))


class TracedProvider:
    """Wraps a provider to record each model call: sizes, timing, the reply."""

    def __init__(self, provider):
        self._provider = provider

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def stream(self, system: str, prompt: str) -> Iterator[str]:
        began = time.monotonic()
        reply: list[str] = []
        try:
            for chunk in self._provider.stream(system, prompt):
                reply.append(chunk)
                yield chunk
        finally:
            event(
                "llm",
                system_chars=len(system),
                prompt_chars=len(prompt),
                seconds=round(time.monotonic() - began, 2),
                resolved_model=getattr(self._provider, "resolved_model", None),
                reply="".join(reply),
            )
