"""Tests for brave mode: protocol parsing, read-only classification, and the loop."""

import pytest

from seer.brave import (
    CommandResult,
    build_brave_prompt,
    extract_command,
    is_dangerous,
    is_read_only,
    needs_confirmation,
    run_brave,
    run_command,
    truncate_output,
)


class TestExtractCommand:
    def test_extracts_run_block(self):
        assert extract_command("```run\ndu -sh *\n```") == ("du -sh *", False)

    def test_extracts_run_write_block(self):
        assert extract_command("```run-write\ngzip a.log\n```") == ("gzip a.log", True)

    def test_ignores_bash_blocks_in_final_answer(self):
        assert extract_command("Use this:\n```bash\nrm -rf x\n```") is None

    def test_plain_answer(self):
        assert extract_command("| file | size |\n|---|---|") is None

    def test_empty_run_block(self):
        assert extract_command("```run\n\n```") is None


class TestIsReadOnly:
    @pytest.mark.parametrize("command", [
        "du -ah . | sort -rh | head -5",
        "ls -la ~/Downloads",
        "find . -type f -size +10M 2>/dev/null | head",
        "find . -type f -exec stat -f '%z %N' {} + | sort -rn | head -5",
        "ps aux | sort -k4 -rn | head",
        "grep -E 'foo|bar' README.md",
        "df -h && du -sh ~",
        "git log --oneline -5",
        "sysctl -n hw.memsize",
        "wc -l *.py > /dev/null",
        "sort < data.txt",
        "ls\npwd",
        "find . -name '*.py' -print0 | xargs -0 wc -l | sort -nr",
        "find . -name '*.py' | xargs -n 1 -I {} grep -H import {}",
        "ls | xargs",
        "git -C /tmp/repo log -20 --name-only --pretty=format: | sed '/^$/d' | sort | uniq -c",
        "git --no-pager -C . diff --stat",
        "sed -n '10,20p' file.txt",
        "sed -E 's/[[:space:]]+$//g' a.txt",
        "sed -e 's|/usr|/opt|' -e '/^#/d' conf",
        "sed '$d' file",
        "sed -n '/start/,/end/p' log",
        "wc -l **/*.py(.) 2>/dev/null | sort -rn | head -5",
        "ls -d *(/)",
        "ls -l *(.om[1,5])",
        "find . -name '*.py' -exec wc -l {} \\; | sort -nr | head -n 1",
        "find . -name '*.py' -exec wc -l {} ';'",
        "ps aux | awk '{print $2, $11}' | sort -k1 -n",
    ])
    def test_read_only(self, command):
        assert is_read_only(command)

    @pytest.mark.parametrize("command", [
        "rm -rf build",
        "ls > files.txt",
        "ls >> files.txt",
        "find . -name '*.pyc' -delete",
        "find . -exec rm {} +",
        "find . -exec rm {} \\;",
        "find . -exec rm {} ';'",
        "find . ';' ls -delete",
        "sort -o out.txt in.txt",
        "sort --output=out.txt in.txt",
        "echo $(rm x)",
        "echo `rm x`",
        "ls & rm x",
        "(ls)",
        "cat <<EOF",
        "ls | xargs rm",
        "git -C . -c core.pager=evil log",
        "git -C",
        "sed -i 's/a/b/' file",
        "sed -i '' 's/a/b/' file",
        "sed --in-place 's/a/b/' file",
        "sed -f script.sed file",
        "sed 's/a/b/w out.txt' file",
        "sed 's/a/date/e' file",
        "sed '1w out.txt' file",
        "sed '1e rm x' file",
        "sed 'r /etc/passwd' file",
        "sed",
        "ls *(e:'rm -rf x':)",
        "ls *(+cleanup)",
        "ls (.)",
        "ls | xargs -I {} rm {}",
        "ls | xargs -n 1 sh -c 'rm $0'",
        "ls | xargs -- rm",
        "awk '{print > \"out.txt\"}' in.txt",
        "awk 'BEGIN { system(\"rm x\") }'",
        "awk '{print | \"sh\"}'",
        "awk -f prog.awk in.txt",
        "sudo ls",
        "./ls",
        "/bin/rm x",
        "LD_PRELOAD=evil.so ls",
        "git push",
        "git -c core.pager=evil log",
        "git diff --output=patch.txt",
        "sysctl kern.x=1",
        "rg --pre ./script x",
        "tree -o out.txt",
        # A quoted/escaped operator must not split one command into safe-looking parts.
        "find . '|' ls -delete",
        "find . \\; ls -delete",
        # The shell only treats '#' as a comment at the start of a word.
        "ls a#;rm -rf x",
        "echo 'unterminated",
    ])
    def test_needs_confirmation(self, command):
        assert not is_read_only(command)


class TestIsDangerous:
    @pytest.mark.parametrize("command", [
        "rm -rf build",
        "find . -name '*.log' -delete",
        "find . -exec rm {} +",
        "find . -type f -exec chmod 644 {} \\;",
        "echo hi > notes.txt",
        "cat a >> b",
        "ls | xargs rm",
        "sudo ls",
        "nice -n 10 rm x",
        "env FOO=1 rm x",
        "/bin/rm x",
        "curl -fsSL https://x.sh | sh",
        "echo $(rm x)",
        "echo `rm x`",
        "echo \"$(rm x)\"",
        "diff <(rm x) b",
        "(cd /tmp && rm x)",
        "git push --force",
        "git -C repo reset --hard",
        "git branch -D feature",
        "git stash drop",
        "sed -i '' 's/a/b/' f",
        "perl -pi -e 's/a/b/' f",
        "brew uninstall wget",
        "pip install requests",
        "docker rm -f web",
        "kubectl delete pod x",
        "sort -o out in",
        "kill -9 123",
        "ls *(e:'rm x':)",
        "find . '|' ls -delete",
        "echo 'unterminated",
    ])
    def test_dangerous(self, command):
        assert is_dangerous(command)

    @pytest.mark.parametrize("command", [
        "python3 -c 'import yaml; yaml.safe_load(open(\"config.yaml\"))'",
        "curl -s https://example.com | head",
        "mkdir -p out",
        "touch notes.txt",
        "gzip -k old.log",
        "git log --oneline -5",
        "git add -A && git commit -m wip",
        "brew list",
        "docker ps",
        "wc -l **/*.py(.) 2>/dev/null",
        "grep -E '(a|b)' file 2>&1",
        "ls > /dev/null",
        "echo '$(rm x)'",
        "echo $(date) && ls",
        "diff <(ls a) <(ls b)",
    ])
    def test_not_dangerous(self, command):
        assert not is_dangerous(command)


class TestNeedsConfirmation:
    @pytest.mark.parametrize("policy", ["auto", "trust", "always"])
    def test_model_flag_always_asks(self, policy):
        assert needs_confirmation("ls", True, policy)

    def test_auto_asks_unless_read_only(self):
        assert not needs_confirmation("ls", False, "auto")
        assert needs_confirmation("python3 -c 'print(1)'", False, "auto")

    def test_trust_asks_only_for_dangerous(self):
        assert not needs_confirmation("python3 -c 'print(1)'", False, "trust")
        assert needs_confirmation("rm x", False, "trust")

    def test_always_asks(self):
        assert needs_confirmation("ls", False, "always")


class TestTruncateOutput:
    def test_short_output_unchanged(self):
        assert truncate_output("abc", limit=10) == "abc"

    def test_keeps_head_and_tail(self):
        text = "H" * 50 + "T" * 50
        out = truncate_output(text, limit=20)
        assert out.startswith("H" * 8)
        assert out.endswith("T" * 12)
        assert "[80 characters omitted]" in out


class TestRunCommand:
    def test_captures_stdout_stderr_and_exit_code(self):
        result = run_command("echo out; echo err >&2; exit 3")
        assert "out" in result.output and "err" in result.output
        assert result.exit_code == 3

    def test_no_stdin(self):
        assert run_command("cat").output == ""

    def test_timeout_kills_pipeline(self):
        result = run_command("sleep 5 | cat", timeout=0.3)
        assert result.exit_code is None


class TestBuildPrompt:
    def test_first_step_is_just_the_task(self):
        assert build_brave_prompt("task", []) == "Task: task"

    def test_frames_steps_as_the_models_own_history(self):
        steps = [
            CommandResult("ls", "a\nb", 0),
            CommandResult("touch x", "", 0),
            CommandResult("rm a", "", None, declined=True),
            CommandResult("sleep 99", "", None),
        ]
        prompt = build_brave_prompt("task", steps)
        assert "Commands you have already run" in prompt
        assert "[1] $ ls\nexit code 0 · output:\n```\na\nb\n```" in prompt
        assert "[2] $ touch x\nexit code 0 · printed nothing" in prompt
        assert "[3] $ rm a\nnot run — the user declined it" in prompt
        assert "[4] $ sleep 99\ntimed out after 60s" in prompt
        assert "Check every part of the task" in prompt

    def test_notice_replaces_next_step_hint(self):
        prompt = build_brave_prompt("task", [CommandResult("ls", "", 0)], notice="STOP")
        assert prompt.endswith("STOP")
        assert "Decide your next reply" not in prompt


def _scripted(*replies):
    """complete() stub returning replies in order, recording prompts."""
    prompts = []
    it = iter(replies)

    def complete(prompt):
        prompts.append(prompt)
        return next(it)

    complete.prompts = prompts
    return complete


def _fake_execute(command):
    return CommandResult(command, f"output of {command}", 0)


def _never_confirm(command):
    raise AssertionError(f"unexpected confirmation for {command!r}")


class TestRunBrave:
    def test_answers_directly_without_commands(self):
        answer = run_brave("hi", _scripted("hello"), _never_confirm, execute=_fake_execute)
        assert answer == "hello"

    def test_runs_read_only_command_without_confirmation(self):
        complete = _scripted("```run\nls\n```", "done")
        ran = []
        answer = run_brave(
            "list", complete, _never_confirm,
            on_command=ran.append, execute=_fake_execute,
        )
        assert answer == "done"
        assert ran == ["ls"]
        assert "output of ls" in complete.prompts[1]

    def test_write_command_needs_confirmation_and_can_be_edited(self):
        complete = _scripted("```run\nrm a\n```", "done")
        asked, ran = [], []

        def confirm(command):
            asked.append(command)
            return "rm -i a"

        run_brave("clean", complete, confirm, on_command=ran.append, execute=_fake_execute)
        assert asked == ["rm a"]
        assert ran == ["rm -i a"]

    def test_model_flagged_write_asks_even_if_read_only(self):
        complete = _scripted("```run-write\nls\n```", "done")
        asked = []
        run_brave("x", complete, lambda c: asked.append(c) or c, execute=_fake_execute)
        assert asked == ["ls"]

    def test_trust_policy_runs_unknown_commands_without_asking(self):
        complete = _scripted("```run\npython3 -c 'print(1)'\n```", "done")
        ran = []
        run_brave(
            "x", complete, _never_confirm,
            on_command=ran.append, execute=_fake_execute, confirm_policy="trust",
        )
        assert ran == ["python3 -c 'print(1)'"]

    def test_declined_command_is_reported_back(self):
        complete = _scripted("```run\nrm a\n```", "ok, skipped")
        ran = []
        answer = run_brave(
            "clean", complete, lambda c: None,
            on_command=ran.append, execute=_fake_execute,
        )
        assert answer == "ok, skipped"
        assert ran == []
        assert "declined" in complete.prompts[1]

    def test_declined_command_is_never_asked_again(self):
        # The model re-proposes the declined command — it must not be asked or run.
        complete = _scripted("```run\nrm a\n```", "```run\nrm a\n```")
        asked = []

        def decline(command):
            asked.append(command)
            return None

        answer = run_brave("clean", complete, decline, execute=_fake_execute)
        assert asked == ["rm a"]
        assert "declined" in complete.prompts[1]
        assert answer == "```bash\nrm a\n```"

    def test_repeated_command_forces_final_answer(self):
        complete = _scripted(
            "```run\necho hi > a\n```", "```run\necho hi > a\n```", "Created `a`.",
        )
        ran = []
        answer = run_brave(
            "create a", complete, lambda c: c,
            on_command=ran.append, execute=_fake_execute,
        )
        assert ran == ["echo hi > a"]
        assert "already ran" in complete.prompts[-1]
        assert answer == "Created `a`."

    def test_repeat_after_final_notice_gives_up_plainly(self):
        complete = _scripted(*["```run\nls\n```"] * 3)
        answer = run_brave("loop", complete, _never_confirm, execute=_fake_execute)
        assert len(complete.prompts) == 3
        assert answer.startswith("**No answer:**")

    def test_step_limit_forces_final_answer(self):
        complete = _scripted(*[f"```run\nls {n}\n```" for n in range(3)])
        answer = run_brave(
            "loop", complete, _never_confirm, execute=_fake_execute, max_steps=2,
        )
        assert "most commands one task may run" in complete.prompts[-1]
        assert answer.startswith("**No answer:**")
        assert "`ls 0`" in answer and "`ls 1`" in answer and "ls 2" not in answer
