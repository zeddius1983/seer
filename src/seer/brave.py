"""
Brave mode: seer runs the commands needed to answer a query, then answers.

The LLM drives a plain-text protocol that works with every provider: a reply
containing a ```run block asks seer to execute that command; a reply without
one is the final answer. Each step resends the full transcript, since
providers are stateless.

The model tags each command ```run (reads only) or ```run-write (changes
something). Whether seer asks before running it depends on `brave_confirm`:

  auto    ask unless the command is provably read-only (`is_read_only`)
  trust   ask only for commands matching known-destructive patterns (`is_dangerous`)
  always  ask for every command

In every mode a ```run-write tag also asks: the model can add confirmations,
never remove them — its judgement can be wrong, or steered by command output.
"""

import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

MAX_STEPS = 6
COMMAND_TIMEOUT = 60.0
MAX_OUTPUT_CHARS = 4000

CONFIRM_POLICIES = ("auto", "trust", "always")

# The block runs to the LAST closing fence of the same length: a command that
# writes Markdown contains ``` fences of its own. Replies with a command contain
# nothing but the block, so nothing legitimate follows it. Longer fences
# (````run … ````) are accepted too — the standard way to wrap text with ```.
_RUN_BLOCK = re.compile(r"^(`{3,})run(-write)?[ \t]*\n(.*)\n\1[ \t]*$", re.DOTALL | re.MULTILINE)

# Commands that only read state, whatever their arguments — unless a flag in
# _WRITE_FLAGS says otherwise. Commands that can run other commands or scripts
# (find -exec, xargs, awk, sed, git) get their own checks below. The rest (env,
# perl, …) and anything that sets state (hostname, ifconfig) is excluded.
_READ_ONLY = {
    "basename", "cat", "cmp", "column", "cut", "date", "df", "diff", "dirname",
    "du", "echo", "egrep", "fgrep", "file", "find", "free", "grep", "head",
    "id", "jq", "ls", "lsof", "md5", "md5sum", "nl", "numfmt", "printf", "ps",
    "pwd", "readlink", "realpath", "rg", "sha1sum", "sha256sum", "shasum",
    "sort", "stat", "sw_vers", "sysctl", "tail", "tr", "tree", "type", "uname",
    "uniq", "uptime", "vm_stat", "wc", "which", "whoami",
}

# Flags that make an otherwise read-only command write files or run programs.
_WRITE_FLAGS = {
    "date": ("-s", "--set"),
    "file": ("-C", "--compile"),
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "rg": ("--pre",),
    "sort": ("-o", "--output", "--compress-program"),
    "sysctl": ("-w", "--write"),
    "tree": ("-o",),
}

# xargs options that take a separate value (-n 1, -I {}); the command follows them.
_XARGS_VALUE_OPTS = {"-a", "-d", "-E", "-e", "-I", "-i", "-J", "-L", "-l", "-n", "-P", "-R", "-S", "-s"}

# awk programs can write files (print > f), run commands (system, | getline)
# or load code (-f). Refuse any program mentioning these.
_AWK_UNSAFE = re.compile(r"system|getline|[>|]")

# sed scripts are safe when, after removing s/// commands and /regex/ addresses,
# only line numbers and print/delete-style commands remain. The s/// flags
# matched exclude w and e, so those are left behind and refused, as are the
# w, W, e, r, a, i, c commands.
_SED_SUBST = re.compile(r"s([^\s\\\w])(?:\\.|(?!\1).)*\1(?:\\.|(?!\1).)*\1[gpiIm0-9]*")
_SED_REGEX_ADDRESS = re.compile(r"\\?/(?:\\.|[^/])*/[IM]*")
_SED_SAFE_REST = re.compile(r"[\d$,!;{}\s~+pdDqQ=nNhHgGxlz]*")
_SED_SAFE_FLAGS = set("nErsuz")

_GIT_READ_ONLY = {"status", "log", "diff", "show", "ls-files", "rev-parse", "blame", "shortlog", "describe"}

# Command separators. Background jobs (&) and subshells are not auto-run.
_SEPARATORS = {"|", "||", "&&", ";"}
_SAFE_REDIRECT_TARGETS = {"/dev/null", "1", "2"}
_OPERATOR_CHARS = "<>&|();"
# A quoted or escaped operator ("|", \;) lexes the same as a real one, which
# could hide arguments from the check — refuse rather than guess.
_AMBIGUOUS_OPERATOR = re.compile(r"""(['"])[<>&|();]+\1|\\[<>&|();]""")
# find's `-exec … \;` terminator is a quoted `;` too. It's swapped for a
# placeholder word first — to the shell it is just an argument, not a separator.
_FIND_EXEC_END = "\0exec-end\0"   # NUL can't occur in a real command
_QUOTED_SEMICOLON = re.compile(r"""(?<=\s)(?:\\;|';'|";")(?=\s|$)""")
# zsh glob qualifiers that only filter or sort matches: **/*.py(.), *(/), *(om[1,5]).
# Others run code — (e:'cmd':), (+func) — so they are left in and refused as `(`.
_SAFE_GLOB_QUALIFIER = re.compile(r"(\S*[*?\]]\S*?)\((?:[./@=p*%rwxND^-]|[oO][nLmacd]|\[\d+(?:,\d+)?\])+\)(?=[\s|;&<>]|$)")

# --- brave_confirm: trust — known state-changing patterns -------------------

_DANGEROUS = {
    "rm", "rmdir", "unlink", "mv", "cp", "ln", "dd", "shred", "srm", "truncate",
    "fdisk", "diskutil", "mount", "umount", "chmod", "chown", "chgrp", "chflags",
    "kill", "killall", "pkill", "sudo", "su", "doas", "shutdown", "reboot", "halt",
    "poweroff", "launchctl", "systemctl", "service", "crontab", "tee", "install",
    "rsync", "sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "source", ".",
}
# Prefixes that run the command after them: `nice rm …` is `rm …`.
_WRAPPERS = {"env", "nice", "nohup", "time", "command", "builtin", "exec", "timeout", "stdbuf", "caffeinate", "watch"}
_GIT_DANGEROUS = {
    "push", "reset", "clean", "checkout", "restore", "switch", "rebase", "rm", "mv",
    "filter-branch", "filter-repo", "gc", "prune", "reflog", "update-ref",
}
# Tools whose subcommands install, remove or destroy things.
_TOOL_DANGEROUS = {
    **dict.fromkeys(
        ("brew", "apt", "apt-get", "dnf", "yum", "pacman", "port", "snap", "pip", "pip3",
         "npm", "pnpm", "yarn", "gem", "cargo", "uv"),
        {"install", "uninstall", "remove", "rm", "upgrade", "update", "purge", "autoremove",
         "cleanup", "reinstall", "add", "sync", "link", "unlink", "-S", "-R", "-Syu"},
    ),
    **dict.fromkeys(
        ("docker", "podman", "kubectl"),
        {"rm", "rmi", "kill", "stop", "prune", "delete", "down", "apply", "run", "exec"},
    ),
}


@dataclass
class CommandResult:
    command: str
    output: str
    exit_code: Optional[int]   # None when the command timed out
    declined: bool = False


def extract_command(reply: str) -> Optional[tuple[str, bool]]:
    """Return (command, flagged_write) from the first ```run / ```run-write block, or None."""
    match = _RUN_BLOCK.search(reply)
    if not match or not match.group(3).strip():
        return None
    return match.group(3).strip(), bool(match.group(2))


def needs_confirmation(command: str, flagged_write: bool, policy: str = "auto") -> bool:
    """Whether to ask before running command under the given brave_confirm policy."""
    if policy == "always" or flagged_write:
        return True
    if policy == "trust":
        return is_dangerous(command)
    return not is_read_only(command)


def _tokenize(command: str) -> Optional[list[str]]:
    """Split command into words and operators the way the shell would, or None.

    None when quoting is unbalanced, or an operator is quoted or escaped
    ("|", \\;) — it lexes like a real one and could hide arguments from a check.
    """
    command = _QUOTED_SEMICOLON.sub(_FIND_EXEC_END, command)
    command = _SAFE_GLOB_QUALIFIER.sub(r"\1", command)
    if _AMBIGUOUS_OPERATOR.search(command):
        return None
    normalized = command.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(normalized, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""   # shlex would end a word at '#'; the shell doesn't
        return list(lexer)
    except ValueError:
        return None


def is_read_only(command: str) -> bool:
    """True only if every part of the command is known not to change anything."""
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        return False
    tokens = _tokenize(command)
    if tokens is None:
        return False

    segment: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _SEPARATORS:
            if not _segment_is_read_only(segment):
                return False
            segment = []
        elif token and set(token) <= set(_OPERATOR_CHARS):
            # Redirections: only 2>&1, >/dev/null and friends are harmless.
            if ">" in token:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if target not in _SAFE_REDIRECT_TARGETS:
                    return False
                i += 1
            elif token != "<":
                return False   # &, (, ), <<, etc.
        else:
            segment.append(token)
        i += 1
    return _segment_is_read_only(segment)


def _segment_is_read_only(words: list[str]) -> bool:
    if not words:
        return True
    name, args = words[0], words[1:]
    # VAR=value prefixes (LD_PRELOAD=…) and paths (./ls) can run anything.
    if "=" in name or "/" in name:
        return False
    if name == "git":
        # Global options can run programs (-c, --exec-path); only -C <dir> and
        # --no-pager may come before the subcommand.
        while args[:1] == ["--no-pager"] or (args[:1] == ["-C"] and len(args) > 1):
            args = args[1:] if args[0] == "--no-pager" else args[2:]
        return (
            bool(args) and args[0] in _GIT_READ_ONLY
            and not any(a.startswith("--output") for a in args)
        )
    if name == "sed":
        return _sed_is_read_only(args)
    if name == "xargs":
        return _xargs_is_read_only(args)
    if name == "awk":
        return not any(a.startswith("-f") or _AWK_UNSAFE.search(a) for a in args)
    if name not in _READ_ONLY:
        return False
    if name == "find":
        args = _strip_read_only_find_exec(args)
        if args is None:
            return False
    if name == "sysctl" and any("=" in a for a in args):
        return False   # `sysctl name=value` sets a value
    forbidden = _WRITE_FLAGS.get(name, ())
    return not any(a == f or a.startswith(f + "=") for a in args for f in forbidden)


def _strip_read_only_find_exec(args: list[str]) -> Optional[list[str]]:
    """Drop `-exec <read-only cmd> {} +` (or `… \\;`) clauses from find's args.

    Returns None if a clause runs something that isn't read-only.
    """
    kept: list[str] = []
    i = 0
    while i < len(args):
        if args[i] in ("-exec", "-execdir"):
            end = next(
                (j for j in range(i + 1, len(args)) if args[j] in ("+", _FIND_EXEC_END)),
                None,
            )
            if end is None:
                return None
            if not _segment_is_read_only(args[i + 1:end]) or end == i + 1:
                return None
            i = end + 1
            continue
        kept.append(args[i])
        i += 1
    return kept


def _sed_is_read_only(args: list[str]) -> bool:
    """sed without -i/-f, running only scripts that print or delete lines."""
    scripts: list[str] = []
    files: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--expression"):
            if i + 1 == len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        if arg.startswith("--expression="):
            scripts.append(arg.split("=", 1)[1])
        elif arg in ("--quiet", "--silent", "--regexp-extended", "--separate", "--unbuffered"):
            pass
        elif arg.startswith("-") and len(arg) > 1:
            if not set(arg[1:]) <= _SED_SAFE_FLAGS:   # -i, -I, -f, --in-place, …
                return False
        else:
            files.append(arg)
        i += 1
    if not scripts:
        if not files:
            return False
        scripts.append(files.pop(0))
    return all(_sed_script_is_read_only(script) for script in scripts)


def _sed_script_is_read_only(script: str) -> bool:
    rest = _SED_REGEX_ADDRESS.sub("", _SED_SUBST.sub("", script))
    return _SED_SAFE_REST.fullmatch(rest) is not None


def _xargs_is_read_only(args: list[str]) -> bool:
    """xargs is as safe as the command it runs (echo when none is given)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--":
            i += 1
            break
        if args[i] in _XARGS_VALUE_OPTS:
            i += 1   # the option's value is the next word
        i += 1
    return _segment_is_read_only(args[i:])


def is_dangerous(command: str) -> bool:
    """True if any part of the command matches a known state-changing pattern.

    The floor under `brave_confirm: trust`. Unlike `is_read_only` it lets
    unknown commands through, so it errs towards True whenever parsing is unsure.
    """
    command = _flatten_substitutions(_SAFE_GLOB_QUALIFIER.sub(r"\1", command))
    if command is None:
        return True
    tokens = _tokenize(command)
    if tokens is None:
        return True

    segment: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _SEPARATORS or token == "&":
            if _segment_is_dangerous(segment):
                return True
            segment = []
        elif token and set(token) <= set(_OPERATOR_CHARS):
            if ">" in token:   # writing to a file (not /dev/null or a stream)
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if target not in _SAFE_REDIRECT_TARGETS:
                    return True
                i += 1
        else:
            segment.append(token)
        i += 1
    return _segment_is_dangerous(segment)


def _flatten_substitutions(command: str) -> Optional[str]:
    """Turn $(…), `…`, <(…) and (…) outside quotes into separate commands.

    Their contents run too, so `echo $(rm x)` becomes `echo  ; rm x ; ` and each
    part is checked. Returns None where that can't be done safely: a
    substitution inside double quotes (it still runs there), or a `(` glued to
    a word — a zsh glob qualifier that can run code, or a function definition.
    """
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'":
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and (ch == "`" or command.startswith("$(", i)):
                return None
            out.append(ch)
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "`" or ch == ")":
            out.append(" ; ")
        elif ch in "$<>" and command.startswith("(", i + 1):
            out.append(" ; ")
            i += 1
        elif ch == "(":
            if i and not command[i - 1].isspace() and command[i - 1] not in "|;&":
                return None
            out.append(" ; ")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _segment_is_dangerous(words: list[str]) -> bool:
    # Skip VAR=value prefixes and wrappers (with their options) to the real command.
    while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0])):
        words = words[1:]
    if not words:
        return False
    name, args = os.path.basename(words[0]), words[1:]
    if name in _WRAPPERS:
        while args and (args[0].startswith("-") or "=" in args[0] or args[0].replace(".", "").isdigit()):
            args = args[1:]
        return _segment_is_dangerous(args)
    if name == "xargs":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in _XARGS_VALUE_OPTS else 1
        return _segment_is_dangerous(args[i:])
    if name in _DANGEROUS or name.startswith("mkfs"):
        return True
    if name == "git":
        while args and args[0] in ("-C", "-c", "--no-pager", "--git-dir", "--work-tree"):
            args = args[1:] if args[0] == "--no-pager" else args[2:]
        sub, rest = (args[0], args[1:]) if args else ("", [])
        return (
            sub in _GIT_DANGEROUS
            or (sub in ("branch", "tag") and any(a in ("-d", "-D", "--delete", "-f", "--force") for a in rest))
            or (sub == "stash" and any(a in ("drop", "clear") for a in rest))
        )
    if name in _TOOL_DANGEROUS:
        return any(a in _TOOL_DANGEROUS[name] for a in args)
    if name == "find":
        for i, arg in enumerate(args):
            if arg in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
                return True
            if arg in ("-exec", "-execdir", "-ok", "-okdir"):
                end = next((j for j in range(i + 1, len(args)) if args[j] in ("+", _FIND_EXEC_END)), len(args))
                if _segment_is_dangerous(args[i + 1:end]):
                    return True
        return False
    if name == "sed":
        return not _sed_is_read_only(args)
    if name == "awk":
        return any(a.startswith("-f") or _AWK_UNSAFE.search(a) for a in args)
    if name in ("perl", "ruby"):
        return any(re.match(r"^-[a-zA-Z]*i", a) for a in args)   # in-place edit
    if name == "sysctl" and any("=" in a for a in args):
        return True
    forbidden = _WRITE_FLAGS.get(name, ())
    return any(a == f or a.startswith(f + "=") for a in args for f in forbidden)


def run_command(command: str, timeout: float = COMMAND_TIMEOUT) -> CommandResult:
    """Run command in the user's shell, non-interactively, capturing all output."""
    shell = os.environ.get("SHELL") or shutil.which("bash") or "sh"
    # Own process group, so a timeout kills the whole pipeline — not just the
    # shell, which would leave children holding the output pipe open.
    proc = subprocess.Popen(
        [shell, "-c", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        exit_code: Optional[int] = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, _ = proc.communicate()
        exit_code = None
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    return CommandResult(command, (out or b"").decode("utf-8", errors="replace"), exit_code)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Keep the head and tail of long output — errors tend to be at the end."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n… [{omitted} characters omitted] …\n{text[-tail:]}"


_NEXT_STEP = (
    "Decide your next reply. Check every part of the task against the results above. "
    "If any part is still unanswered, run the next command needed (never repeat one that "
    "already succeeded). If everything is answered, give the final answer now — no run "
    "block; if the task changed something, say what was done rather than just repeating "
    "the output."
)
_DECLINED = (
    "The user declined the last command, so stop: do not run or propose any more commands. "
    "Give the final answer now, addressing the user as \"you\": say briefly what was not "
    "done, and show the command in a ```bash block so they can run it themselves."
)
_REPEATED = (
    "You just proposed a command that already ran — its result is above. Do not run any "
    "more commands; give the final answer now from the results above."
)
_STEP_LIMIT = (
    "That is the most commands one task may run. Do not run any more; give the final "
    "answer now from the results above, and mention anything you could not find out."
)


def build_brave_prompt(task: str, steps: list[CommandResult], notice: Optional[str] = None) -> str:
    """The task plus every command run so far, framed as the model's own history.

    Providers are single-turn, so without the framing models read the transcript
    as part of the task and re-run commands that already succeeded.
    """
    parts = [f"Task: {task}"]
    if not steps:
        return parts[0]
    parts.append("Commands you have already run for this task, with their results:")
    for n, step in enumerate(steps, 1):
        if step.declined:
            parts.append(f"[{n}] $ {step.command}\nnot run — the user declined it")
            continue
        status = (
            f"timed out after {COMMAND_TIMEOUT:.0f}s" if step.exit_code is None
            else f"exit code {step.exit_code}"
        )
        output = truncate_output(step.output.strip())
        body = f"{status} · output:\n```\n{output}\n```" if output else f"{status} · printed nothing"
        parts.append(f"[{n}] $ {step.command}\n{body}")
    parts.append(notice or _NEXT_STEP)
    return "\n\n".join(parts)


def _gave_up(steps: list[CommandResult]) -> str:
    ran = "\n".join(f"- `{step.command}`" for step in steps if not step.declined)
    return (
        f"**No answer:** the model ran {len(steps)} command(s) but couldn't finish the task.\n\n"
        f"{ran}\n\n"
        "Small local models often struggle with multi-step tasks — try a larger one "
        "(`seer -p <provider> -b …`) or rephrase the task more specifically."
    )


def run_brave(
    task: str,
    complete: Callable[[str], str],
    confirm: Callable[[str], Optional[str]],
    on_command: Callable[[str], None] = lambda command: None,
    on_result: Callable[[CommandResult], None] = lambda result: None,
    execute: Callable[[str], CommandResult] = run_command,
    max_steps: int = MAX_STEPS,
    confirm_policy: str = "auto",
) -> str:
    """Drive the brave-mode loop and return the final answer (Markdown).

    complete(prompt) -> the LLM's full reply.
    confirm(command) -> the command to run (possibly edited), or None to decline.
    """
    steps: list[CommandResult] = []
    notice: Optional[str] = None
    while True:
        # A declined command ends the run: the user said no, so wrap up rather
        # than let the model re-propose it (or a variant) and ask again.
        if notice is None and steps and steps[-1].declined:
            notice = _DECLINED
        elif notice is None and len(steps) >= max_steps:
            notice = _STEP_LIMIT
        reply = complete(build_brave_prompt(task, steps, notice))
        proposed = extract_command(reply)
        if proposed is None:
            return reply
        command, flagged_write = proposed
        if notice is _DECLINED:
            # Asked for a command anyway — show it for the user to run, never run it.
            return _RUN_BLOCK.sub(lambda m: f"{m.group(1)}bash\n{m.group(3)}\n{m.group(1)}", reply)
        if notice is not None:
            # Looping or out of steps, and still no answer — say so plainly.
            return _gave_up(steps)
        if any(step.command == command for step in steps):
            # Re-running it can't tell us anything new; the model is looping.
            notice = _REPEATED
            continue

        if needs_confirmation(command, flagged_write, confirm_policy):
            approved = confirm(command)
            if approved is None:
                steps.append(CommandResult(command, "", None, declined=True))
                continue
            command = approved
        on_command(command)
        result = execute(command)
        on_result(result)
        steps.append(result)
