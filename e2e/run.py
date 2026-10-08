#!/usr/bin/env python3
"""
Run seer's end-to-end scenarios against one provider/model and record what happened.

  .venv/bin/python e2e/run.py                                # your configured provider
  .venv/bin/python e2e/run.py -p ollama -m gemma4:26b --repeat 3
  .venv/bin/python e2e/run.py -p claude-cli --only brave --only plain-question
  .venv/bin/python e2e/run.py --list
  .venv/bin/python e2e/run.py -v ...      # each model call, command and prompt as it happens
  .venv/bin/python e2e/run.py -vv ...     # the scenario's terminal, live

e2e/run.sh does the same, syncing the virtualenv first.
  .venv/bin/python e2e/run.py -v ...   # each model call, command and prompt as it happens
  .venv/bin/python e2e/run.py -vv ...  # the scenario's terminal, live

Each run gets a fresh fixture workspace (fixtures.py), its own cache directory
(so `seer help` sees the scenario's context, not yours) and a pseudo-terminal
(so [Y/n/e] prompts work and are answered from the scenario). seer writes a
trace (SEER_TRACE, see src/seer/trace.py) that the checks are evaluated on.

Results: e2e/results/<label>/<timestamp>/ — summary.md, summary.json, and per
run <scenario>/<n>/{output.txt, trace.jsonl, result.json}. GRADING.md tells an
LLM how to grade them against the scenarios' `expect` lines.
"""

import argparse
import datetime
import fcntl
import json
import os
import pty
import re
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

import yaml

import fixtures

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_TIMEOUT = 180
TERMINAL_SIZE = (40, 100)   # rows, columns

_PROMPT = re.compile(rb"Run this command\? \[Y/n/e\]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-B]")
_EDIT_PROMPT = re.compile(rb"\$ ")   # the inline editor's prompt after answering e
_CHECKS = {
    "mode", "error", "answer_contains", "answer_excludes", "output_contains", "output_excludes",
    "max_llm_calls", "max_commands", "confirmations", "min_confirmations",
    "commands_include", "commands_exclude", "files_exist", "files_absent",
}
_FIELDS = {"id", "features", "run", "answers", "context", "config", "timeout", "checks", "expect"}
_KEYS = {"y": "y\r", "n": "n\r", "ctrl-c": "\x03"}


# --- scenarios ---------------------------------------------------------------

def load_scenarios(path: Path = HERE / "scenarios.yaml") -> list[dict]:
    scenarios = yaml.safe_load(path.read_text())
    seen = set()
    for sc in scenarios:
        problems = [f"unknown field '{k}'" for k in sc if k not in _FIELDS]
        problems += [f"unknown check '{k}'" for k in sc.get("checks", {}) if k not in _CHECKS]
        if not sc.get("id") or not sc.get("run"):
            problems.append("needs an id and a run command")
        if sc.get("id") in seen:
            problems.append("duplicate id")
        for answer in sc.get("answers", []):
            if not (set(answer) == {"edit"} if isinstance(answer, dict) else answer in _KEYS):
                problems.append(f"bad answer {answer!r} (y, n, ctrl-c or {{edit: <command>}})")
        if problems:
            raise ValueError(f"scenario {sc.get('id', '?')}: {'; '.join(problems)}")
        seen.add(sc["id"])
    return scenarios


def choose(scenarios: list[dict], only: list[str]) -> list[dict]:
    """Scenarios whose id or one of whose features is in only (all if empty)."""
    if not only:
        return scenarios
    chosen = [sc for sc in scenarios if sc["id"] in only or set(sc.get("features", [])) & set(only)]
    if not chosen:
        raise SystemExit(f"No scenario matches {only}. See --list.")
    return chosen


# --- running -----------------------------------------------------------------

def run_scenario(sc: dict, seer: str, provider: str, model: str, verbose: int = 0) -> dict:
    """Run one scenario in a fresh workspace; returns its artifacts and metrics.

    verbose 1 prints each trace event as seer writes it; 2 mirrors the
    scenario's terminal instead.
    """
    with tempfile.TemporaryDirectory(prefix="seer-e2e-") as tmp:
        tmp = Path(tmp)
        project = fixtures.build(tmp)
        cache = tmp / "cache"
        (cache / "seer").mkdir(parents=True)
        if sc.get("context"):
            (cache / "seer" / "context").write_text(sc["context"])
        bindir = _seer_shim(tmp / "bin", seer, provider, model)
        trace_path = tmp / "trace.jsonl"

        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "SEER_TRACE")}
        if sc.get("config"):
            env["XDG_CONFIG_HOME"] = str(_scenario_config(tmp / "config", sc["config"]))
        env.update({
            "PATH": f"{bindir}{os.pathsep}{env.get('PATH', '')}",
            "XDG_CACHE_HOME": str(cache),
            "SEER_TRACE": str(trace_path),
            "TERM": "xterm-256color",
            "COLUMNS": str(TERMINAL_SIZE[1]),
            "LINES": str(TERMINAL_SIZE[0]),
        })
        tail = TraceTail(trace_path)

        def print_events():
            for event in tail.poll():
                line = describe(event)
                if line:
                    # commands seer ran: dim, as seer itself shows them
                    print(f"    {_dim(line) if event.get('event') == 'command' else line}", flush=True)

        started = time.monotonic()
        raw, exit_code, timed_out, answered = _run_in_pty(
            sc["run"], project, env, list(sc.get("answers", [])), sc.get("timeout", DEFAULT_TIMEOUT),
            on_data=_mirror if verbose >= 2 else None,
            on_tick=print_events if verbose == 1 else None,
        )
        if verbose == 1:
            print_events()   # whatever seer wrote after the last tick
        seconds = round(time.monotonic() - started, 1)
        events = _read_trace(trace_path)
        metrics = summarize(events)
        output = screen_text(raw)
        results = evaluate(sc.get("checks", {}), metrics, project, timed_out, output)
        return {
            "id": sc["id"],
            "passed": all(r["ok"] for r in results),
            "checks": results,
            "metrics": metrics,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "seconds": seconds,
            "answers_given": answered,
            "output": output,
            "trace": events,
        }


def _scenario_config(config_home: Path, overrides: dict) -> Path:
    """Your seer config with the scenario's settings on top, in a config home
    of its own. Top-level keys replace yours; `providers` entries are merged."""
    user_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    user_file = user_home / "seer" / "config.yaml"
    config = (yaml.safe_load(user_file.read_text()) or {}) if user_file.exists() else {}
    for key, value in overrides.items():
        if key == "providers":
            config["providers"] = {**config.get("providers", {}), **value}
        else:
            config[key] = value
    (config_home / "seer").mkdir(parents=True)
    (config_home / "seer" / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    return config_home


def _seer_shim(bindir: Path, seer: str, provider: str, model: str) -> Path:
    """A `seer` on PATH that runs the build under test with -p/-m added."""
    bindir.mkdir()
    flags = (["-p", provider] if provider else []) + (["-m", model] if model else [])
    shim = bindir / "seer"
    shim.write_text(f"#!/bin/sh\nexec {shlex.join([seer, *flags])} \"$@\"\n")
    shim.chmod(0o755)
    return bindir


def _run_in_pty(command: str, cwd: Path, env: dict, answers: list, timeout: float,
                on_data=None, on_tick=None):
    """Run command with bash in a pseudo-terminal, answering [Y/n/e] prompts.

    An answer is "y", "n", "ctrl-c", or {"edit": cmd}: press e, then replace
    the command in the inline editor once its prompt appears. Prompts beyond
    the scenario's answers get "n", so nothing runs that the scenario didn't
    approve. Returns (raw output, exit code, timed out, answers given).
    on_data(bytes) sees the output as it arrives; on_tick() runs at least
    every half second.
    """
    pid, fd = pty.fork()
    if pid == 0:   # child: the session leader of a new terminal
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", *TERMINAL_SIZE, 0, 0))
            os.chdir(cwd)
            os.execvpe("bash", ["bash", "-c", command], env)
        finally:
            os._exit(127)

    output = bytearray()
    answered: list = []
    prompts = 0
    scanned = 0      # prompts before this offset are counted
    editing = None   # (replacement, output offset when e was sent)
    deadline = time.monotonic() + timeout
    timed_out = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            os.killpg(pid, signal.SIGKILL)
            break
        ready, _, _ = select.select([fd], [], [], min(remaining, 0.5))
        if on_tick:
            on_tick()
        if not ready:
            continue
        try:
            data = os.read(fd, 65536)
        except OSError:   # EIO: the terminal closed, the command is done
            break
        if not data:
            break
        if on_data:
            on_data(data)
        output += data
        # Only scan what's new (plus a prompt's length, for one split across
        # reads): the status line redraws ~12 times a second, and rescanning
        # everything each time falls behind, until a full terminal buffer
        # blocks seer mid-reply.
        for match in _PROMPT.finditer(output, max(scanned, len(output) - len(data) - 32)):
            prompts += 1
            scanned = match.end()
        if editing and _EDIT_PROMPT.search(output, editing[1]):
            # Ctrl+U clears the pre-filled command, then type the replacement.
            os.write(fd, b"\x15" + editing[0].encode() + b"\r")
            editing = None
        while not editing and len(answered) < prompts:
            reply = answers.pop(0) if answers else "n"
            answered.append(reply)
            if isinstance(reply, dict):
                editing = (reply["edit"], len(output))
                os.write(fd, b"e\r")
            else:
                os.write(fd, _KEYS[reply].encode())
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    return bytes(output), os.waitstatus_to_exitcode(status), timed_out, answered


def _dim(text: str) -> str:
    """Grey text on a terminal; plain when piped, saved or NO_COLOR is set."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\x1b[2m{text}\x1b[0m"


def _mirror(data: bytes) -> None:
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


class TraceTail:
    """Reads the events seer has appended to its trace since the last poll."""

    def __init__(self, path: Path):
        self.path, self.offset, self.partial = path, 0, ""

    def poll(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                f.seek(self.offset)
                data = f.read()
                self.offset = f.tell()
        except FileNotFoundError:
            return []
        *lines, self.partial = (self.partial + data).split("\n")
        events = []
        for line in lines:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events


def describe(e: dict):
    """One line for an event, for -v; None for events not worth a line."""
    kind = e.get("event")

    def short(text, limit=110):
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    if kind == "mode":
        piped = e.get("piped_chars") or e.get("context_chars")
        return f"mode: {e.get('mode')}" + (f" ({piped:,} chars of input)" if piped else "")
    if kind == "provider":
        model = str(e.get("model") or "").rsplit("/", 1)[-1]   # local models are often file paths
        return f"provider: {e.get('name')} · {short(model, 70)}"
    if kind == "llm":
        thought = e.get("reasoning_words")
        return (f"model: {e.get('seconds')}s" + (f", {thought:,} words of reasoning" if thought else "")
                + f" → {short(e.get('reply')) or '(empty)'}")
    if kind == "command":
        code = e.get("exit_code")
        status = "timed out" if code is None else f"exit {code}"
        size = f", {e['output_chars']:,} chars" if e.get("output_chars") is not None else ""
        return f"$ {short(e.get('command'), 90)}  → {status}{size}"
    if kind == "confirm":
        return f"[Y/n/e] {short(e.get('command'), 80)}  → {e.get('decision')}"
    if kind == "answer":
        return f"answer: {short(e.get('text'))}"
    if kind in ("error", "interrupted"):
        return f"{kind}: {short(e.get('message', ''))}"
    return None


def screen_text(raw: bytes) -> str:
    """Terminal output as it ended up on screen, roughly: colors dropped, and
    of each line only what follows its last carriage return — the status line
    redraws itself in place, and is cleared at the end."""
    text = _ANSI.sub("", raw.decode("utf-8", errors="replace")).replace("\r\n", "\n")
    lines = [line.rsplit("\r", 1)[-1].rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def _read_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


# --- checking ----------------------------------------------------------------

def summarize(events: list[dict]) -> dict:
    """The facts of a run, from its trace."""
    def of(kind):
        return [e for e in events if e.get("event") == kind]

    modes = of("mode")
    mode = modes[-1]["mode"] if modes else None
    answers = [e.get("text", "") for e in of("answer")]
    llm = of("llm")
    provider = of("provider")
    return {
        "mode": mode,
        "provider": provider[0].get("name") if provider else None,
        "model": provider[0].get("model") if provider else None,
        "resolved_model": next((e["resolved_model"] for e in reversed(llm) if e.get("resolved_model")), None),
        "llm_calls": len(llm),
        "llm_seconds": round(sum(e.get("seconds", 0) for e in llm), 1),
        "max_prompt_chars": max((e.get("prompt_chars", 0) for e in llm), default=0),
        "commands": [e.get("command", "") for e in of("command")],
        "confirmations": len(of("confirm")),
        "edited": sum(1 for e in of("confirm") if e.get("decision") == "edited"),
        "declined": sum(1 for e in of("confirm") if e.get("decision") == "declined"),
        "errors": [e.get("message", "") for e in of("error")],
        # watch mode answers once per batch
        "answer": "\n\n".join(answers) if mode == "watch" else (answers[-1] if answers else ""),
    }


def evaluate(checks: dict, m: dict, project: Path, timed_out: bool, output: str = "") -> list[dict]:
    """Every check as {check, ok, detail}. Timeouts always count, and seer
    errors do unless the scenario expects one (`error: true`)."""
    want_error = checks.get("error", False)
    results = [
        {"check": "finished", "ok": not timed_out, "detail": "timed out" if timed_out else ""},
        {"check": "error" if want_error else "no_errors", "ok": bool(m["errors"]) == want_error,
         "detail": "; ".join(m["errors"]) or ("no error reported" if want_error else "")},
    ]
    answer = m["answer"].lower()
    screen = output.lower()

    def add(name, ok, detail=""):
        results.append({"check": name, "ok": bool(ok), "detail": detail})

    for name, want in checks.items():
        if name == "error":
            continue   # handled above
        elif name == "mode":
            add(name, m["mode"] == want, f"got {m['mode']}")
        elif name == "answer_contains":
            missing = [w for w in want if not any(alt.lower() in answer for alt in str(w).split("|"))]
            add(name, not missing, f"missing: {missing}" if missing else "")
        elif name == "answer_excludes":
            found = [w for w in want if str(w).lower() in answer]
            add(name, not found, f"found: {found}" if found else "")
        elif name == "output_contains":
            missing = [w for w in want if not any(alt.lower() in screen for alt in str(w).split("|"))]
            add(name, not missing, f"missing: {missing}" if missing else "")
        elif name == "output_excludes":
            found = [w for w in want if str(w).lower() in screen]
            add(name, not found, f"found: {found}" if found else "")
        elif name == "max_llm_calls":
            add(name, m["llm_calls"] <= want, f"{m['llm_calls']} calls")
        elif name == "max_commands":
            add(name, len(m["commands"]) <= want, f"{len(m['commands'])} commands")
        elif name == "confirmations":
            add(name, m["confirmations"] == want, f"{m['confirmations']} prompts")
        elif name == "min_confirmations":
            add(name, m["confirmations"] >= want, f"{m['confirmations']} prompts")
        elif name == "commands_include":
            missing = [p for p in want if not any(re.search(p, c) for c in m["commands"])]
            add(name, not missing, f"none matched: {missing}" if missing else "")
        elif name == "commands_exclude":
            hits = [c for c in m["commands"] for pattern in want if re.search(pattern, c)]
            add(name, not hits, f"ran: {hits}" if hits else "")
        elif name == "files_exist":
            missing = [p for p in want if not (project / p).exists()]
            add(name, not missing, f"missing: {missing}" if missing else "")
        elif name == "files_absent":
            present = [p for p in want if (project / p).exists()]
            add(name, not present, f"present: {present}" if present else "")
    return results


# --- reporting ---------------------------------------------------------------

def write_run(out: Path, n: int, result: dict) -> None:
    run_dir = out / result["id"] / str(n)
    run_dir.mkdir(parents=True)
    (run_dir / "output.txt").write_text(result["output"])
    (run_dir / "trace.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in result["trace"]))
    record = {k: v for k, v in result.items() if k not in ("output", "trace")}
    (run_dir / "result.json").write_text(json.dumps(record, indent=2, ensure_ascii=False))


def write_summary(out: Path, meta: dict, results: list[dict], scenarios: list[dict]) -> str:
    rows = []
    for sc in scenarios:
        runs = [r for r in results if r["id"] == sc["id"]]
        if not runs:
            continue
        failed = sorted({c["check"] for r in runs for c in r["checks"] if not c["ok"]})
        rows.append({
            "id": sc["id"],
            "passed": sum(r["passed"] for r in runs),
            "runs": len(runs),
            "avg_llm_calls": round(sum(r["metrics"]["llm_calls"] for r in runs) / len(runs), 1),
            "avg_commands": round(sum(len(r["metrics"]["commands"]) for r in runs) / len(runs), 1),
            "avg_seconds": round(sum(r["seconds"] for r in runs) / len(runs), 1),
            "failed_checks": failed,
        })
    total = sum(r["passed"] for r in results)
    (out / "summary.json").write_text(json.dumps({**meta, "scenarios": rows}, indent=2))

    lines = [
        f"# seer e2e — {meta['label']}",
        "",
        f"- seer {meta['seer_version']} ({meta['seer_commit']}), {meta['started']}",
        f"- provider `{meta['provider'] or 'from config'}`, model `{meta['model'] or 'from config'}`"
        + (f" (resolved: `{meta['resolved_model']}`)" if meta.get("resolved_model") else ""),
        f"- mechanical checks passed: **{total}/{len(results)}** runs",
        "",
        "| scenario | passed | calls | commands | seconds | failed checks |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['id']} | {row['passed']}/{row['runs']} | {row['avg_llm_calls']} "
            f"| {row['avg_commands']} | {row['avg_seconds']} | {', '.join(row['failed_checks'])} |"
        )
    text = "\n".join(lines) + "\n"
    (out / "summary.md").write_text(text)
    return text


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "describe", "--always", "--dirty"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run seer's end-to-end scenarios.")
    parser.add_argument("-p", "--provider", help="seer provider to use (default: your config)")
    parser.add_argument("-m", "--model", help="model override for that provider")
    parser.add_argument("--repeat", type=int, default=1, help="runs per scenario (models vary run to run)")
    parser.add_argument("--only", action="append", default=[], help="scenario id or feature (repeatable)")
    parser.add_argument("--label", help="results subdirectory (default: <provider>-<model>)")
    parser.add_argument("--seer", default=str(REPO / ".venv" / "bin" / "seer"), help="seer executable under test")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v: each model call, command and prompt as it happens; -vv: the terminal, live")
    args = parser.parse_args(argv)

    scenarios = choose(load_scenarios(), args.only)
    if args.list:
        for sc in scenarios:
            print(f"{sc['id']:28} {', '.join(sc.get('features', []))}")
        return 0
    if not Path(args.seer).exists():
        raise SystemExit(f"seer not found at {args.seer} — run `uv sync` first, or pass --seer.")

    label = args.label or re.sub(r"[^\w.-]+", "_", f"{args.provider or 'default'}-{args.model or 'default'}")
    started = datetime.datetime.now()
    out = HERE / "results" / label / started.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    shutil.copy(HERE / "scenarios.yaml", out / "scenarios.yaml")   # what these results were graded against
    version = subprocess.run([args.seer, "--version"], capture_output=True, text=True).stdout.strip()

    results = []
    for sc in scenarios:
        for n in range(1, args.repeat + 1):
            if args.verbose:
                print(f"\n── {sc['id']} #{n}: {sc['run'].strip()}", flush=True)
            else:
                print(f"{sc['id']} #{n} … ", end="", flush=True)
            result = run_scenario(sc, args.seer, args.provider, args.model, args.verbose)
            write_run(out, n, result)
            results.append(result)
            m = result["metrics"]
            verdict = (
                f"{'PASS' if result['passed'] else 'FAIL'}  {m['llm_calls']} calls, "
                f"{len(m['commands'])} commands, {result['seconds']}s"
            )
            if args.verbose:
                print(f"\n   {verdict}")
                for check in result["checks"]:
                    if not check["ok"]:
                        print(f"   ✗ {check['check']}" + (f": {check['detail']}" if check["detail"] else ""))
            else:
                failed = [c["check"] for c in result["checks"] if not c["ok"]]
                print(verdict + (f"  failed: {', '.join(failed)}" if failed else ""))

    meta = {
        "label": label,
        "provider": args.provider,
        "model": args.model,
        "resolved_model": next((r["metrics"]["resolved_model"] for r in results if r["metrics"]["resolved_model"]), None),
        "seer_version": version.removeprefix("seer, version "),
        "seer_commit": _git_commit(),
        "started": started.isoformat(timespec="seconds"),
        "repeat": args.repeat,
    }
    print()
    print(write_summary(out, meta, results, scenarios))
    print(f"Results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
