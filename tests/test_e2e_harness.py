"""Tests for the e2e runner's offline parts: scenarios, fixtures, checks, terminal handling."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "e2e"))

import fixtures  # noqa: E402
import run  # noqa: E402


def test_scenarios_load_and_are_valid():
    scenarios = run.load_scenarios()
    assert len(scenarios) >= 15
    assert all(sc.get("expect") for sc in scenarios), "every scenario needs expectations for the grader"


def test_unknown_check_is_rejected(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text("- id: x\n  run: seer hi\n  checks: {answer_containz: [a]}\n")
    with pytest.raises(ValueError, match="unknown check 'answer_containz'"):
        run.load_scenarios(path)


def test_choose_by_id_or_feature():
    scenarios = [{"id": "a", "features": ["brave"]}, {"id": "b", "features": ["plain"]}]
    assert [s["id"] for s in run.choose(scenarios, ["brave"])] == ["a"]
    assert [s["id"] for s in run.choose(scenarios, ["b"])] == ["b"]
    assert run.choose(scenarios, []) == scenarios


def test_fixture_matches_its_facts(tmp_path):
    project = fixtures.build(tmp_path)
    log = (project / "logs" / "big.log").read_text().splitlines()
    errors = [line for line in log if " ERROR " in line]
    assert len(errors) == fixtures.FACTS["big_log_errors"]
    top = sum(fixtures.FACTS["big_log_top_error"] in line for line in errors)
    assert top == fixtures.FACTS["big_log_top_error_count"]
    files = [p for p in project.rglob("*") if p.is_file() and ".git" not in p.parts]
    largest = max(files, key=lambda p: p.stat().st_size)
    assert largest.relative_to(project).as_posix() == fixtures.FACTS["largest_file"]
    log_lines = subprocess.run(["git", "log", "--format=%s"], cwd=project, capture_output=True, text=True).stdout
    assert log_lines.split("\n")[:5] == [
        "Bump version to 0.2.0", "Add average report", "wip",
        "Fix off-by-one in pagination", "Add CLI entry point",
    ]


def test_fixture_commits_are_reproducible(tmp_path):
    heads = []
    for name in ("a", "b"):
        project = fixtures.build(tmp_path / name)
        heads.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                                    capture_output=True, text=True).stdout)
    assert heads[0] == heads[1]


def _metrics(**overrides):
    m = {"mode": "brave", "llm_calls": 2, "commands": ["ls -la"], "confirmations": 0,
         "errors": [], "answer": "The largest is archive.gz (2 MB)."}
    return {**m, **overrides}


def test_evaluate_passes_and_fails(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    checks = {
        "mode": "brave",
        "answer_contains": ["ARCHIVE.GZ", "missing|2 MB"],
        "answer_excludes": ["sample.csv"],
        "max_llm_calls": 2,
        "max_commands": 1,
        "confirmations": 0,
        "commands_exclude": [r"^rm\b"],
        "files_exist": ["keep.txt"],
        "files_absent": ["gone.txt"],
    }
    results = run.evaluate(checks, _metrics(), tmp_path, timed_out=False)
    assert all(r["ok"] for r in results), results

    failed = {r["check"] for r in run.evaluate(
        {"answer_contains": ["csv"], "confirmations": 1, "files_exist": ["gone.txt"]},
        _metrics(errors=["boom"]), tmp_path, timed_out=True,
    ) if not r["ok"]}
    assert failed == {"finished", "no_errors", "answer_contains", "confirmations", "files_exist"}


def test_summarize_reads_the_trace():
    events = [
        {"event": "mode", "mode": "brave"},
        {"event": "provider", "name": "claude-cli", "model": "sonnet"},
        {"event": "llm", "prompt_chars": 40, "seconds": 1.5, "resolved_model": "claude-sonnet-5-5", "reply": "```run\nls\n```"},
        {"event": "command", "command": "ls", "exit_code": 0},
        {"event": "confirm", "command": "rm x", "decision": "declined"},
        {"event": "llm", "prompt_chars": 90, "seconds": 2.0, "resolved_model": "claude-sonnet-5-5", "reply": "done"},
        {"event": "answer", "text": "done"},
    ]
    m = run.summarize(events)
    assert m["llm_calls"] == 2 and m["max_prompt_chars"] == 90 and m["llm_seconds"] == 3.5
    assert m["commands"] == ["ls"] and m["confirmations"] == 1 and m["declined"] == 1
    assert m["resolved_model"] == "claude-sonnet-5-5" and m["answer"] == "done"


def test_screen_text_drops_redrawn_status_lines():
    raw = (b"  thinking\r\x1b[2K  thinking (3 words)\r\x1b[2K\r\n"
           b"  $ ls\r\n\x1b[1mRun this command? [Y/n/e] [y]: \x1b[0mn\r\n    answer\r\n")
    assert run.screen_text(raw) == "$ ls\nRun this command? [Y/n/e] [y]: n\n    answer\n"


def test_pty_answers_prompts_in_order_then_no(tmp_path):
    # Like seer: [Y/n/e], and after e an inline editor pre-filled with the command.
    script = (
        "read -p 'Run this command? [Y/n/e] ' a; echo \"got:$a\";"
        " read -p 'Run this command? [Y/n/e] ' a; echo \"got:$a\";"
        " read -e -i 'rm -rf x' -p '$ ' c; echo \"edited:$c\";"
        " read -p 'Run this command? [Y/n/e] ' a; echo \"got:$a\";"
        " [ -t 0 ] && echo tty"
    )
    raw, exit_code, timed_out, answered = run._run_in_pty(
        script, tmp_path, {"PATH": "/usr/bin:/bin"}, ["y", {"edit": "ls -la"}], 10,
    )
    text = run.screen_text(raw)
    assert answered == ["y", {"edit": "ls -la"}, "n"]
    assert "got:y" in text and "got:e" in text and "edited:ls -la" in text and "got:n" in text
    assert "tty" in text   # the command sees a real terminal
    assert (exit_code, timed_out) == (0, False)


def test_pty_ctrl_c_interrupts(tmp_path):
    script = "trap 'echo interrupted; exit 3' INT; read -p 'Run this command? [Y/n/e] ' a; echo \"got:$a\""
    raw, exit_code, _, answered = run._run_in_pty(script, tmp_path, {"PATH": "/usr/bin:/bin"}, ["ctrl-c"], 10)
    assert answered == ["ctrl-c"]
    assert "interrupted" in run.screen_text(raw) and exit_code == 3


def test_bad_answer_is_rejected(tmp_path):
    path = tmp_path / "scenarios.yaml"
    path.write_text("- id: x\n  run: seer hi\n  answers: [yes]\n")
    with pytest.raises(ValueError, match="bad answer"):
        run.load_scenarios(path)


def test_expected_error_and_output_checks(tmp_path):
    results = run.evaluate(
        {"error": True, "output_contains": ["Error:"], "output_excludes": ["Traceback"],
         "min_confirmations": 1, "commands_include": [r"^ls\b"]},
        _metrics(errors=["model not found"], confirmations=2), tmp_path, timed_out=False,
        output="Error: model not found\n",
    )
    assert all(r["ok"] for r in results), results
    assert {r["check"] for r in results} >= {"error", "output_contains", "min_confirmations"}


def test_scenario_config_overrides_user_config(tmp_path, monkeypatch):
    user = tmp_path / "user"
    (user / "seer").mkdir(parents=True)
    (user / "seer" / "config.yaml").write_text(
        "provider: auto\nbrave: false\nproviders:\n  ollama: {type: openai, model: auto}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    home = run._scenario_config(tmp_path / "scenario", {"brave": True, "providers": {"x": {"type": "openai"}}})
    import yaml
    config = yaml.safe_load((home / "seer" / "config.yaml").read_text())
    assert config["brave"] is True and config["provider"] == "auto"
    assert set(config["providers"]) == {"ollama", "x"}


def test_pty_timeout_kills_the_command(tmp_path):
    _, _, timed_out, _ = run._run_in_pty("sleep 30", tmp_path, {"PATH": "/usr/bin:/bin"}, [], 1)
    assert timed_out


def test_pty_keeps_up_with_heavy_output(tmp_path):
    # A status line redrawn many times a second: the runner must keep reading,
    # or a full terminal buffer blocks the command (seer mid-reply).
    script = (
        "for i in $(seq 1 20000); do printf '\\r  thinking… (%d words)' $i; done;"
        " echo; read -p 'Run this command? [Y/n/e] ' a; echo \"got:$a\""
    )
    raw, _, timed_out, answered = run._run_in_pty(script, tmp_path, {"PATH": "/usr/bin:/bin"}, ["y"], 20)
    assert not timed_out and answered == ["y"]
    assert "got:y" in run.screen_text(raw)


def test_trace_tail_returns_only_new_complete_events(tmp_path):
    path = tmp_path / "trace.jsonl"
    tail = run.TraceTail(path)
    assert tail.poll() == []                       # seer hasn't written yet
    path.write_text('{"event": "mode", "mode": "brave"}\n{"event": "llm", "sec')
    assert [e["event"] for e in tail.poll()] == ["mode"]   # the half-written line waits
    with open(path, "a") as f:
        f.write('onds": 2.5, "reply": "ok"}\n')
    (event,) = tail.poll()
    assert event == {"event": "llm", "seconds": 2.5, "reply": "ok"}
    assert tail.poll() == []


@pytest.mark.parametrize("event, line", [
    ({"event": "mode", "mode": "brave", "piped_chars": 1200}, "mode: brave (1,200 chars of input)"),
    ({"event": "provider", "name": "vllm", "model": "/models/x/gemma.gguf"}, "provider: vllm · gemma.gguf"),
    ({"event": "llm", "seconds": 3.6, "reasoning_words": 1500, "reply": "```run\nls\n```"},
     "model: 3.6s, 1,500 words of reasoning → ```run ls ```"),
    ({"event": "command", "command": "ls -R", "exit_code": None, "output_chars": 0}, "$ ls -R  → timed out, 0 chars"),
    ({"event": "confirm", "command": "rm x", "decision": "declined"}, "[Y/n/e] rm x  → declined"),
    ({"event": "answer", "text": "x" * 200}, "answer: " + "x" * 109 + "…"),
    ({"event": "start"}, None),
])
def test_describe(event, line):
    assert run.describe(event) == line


def test_pty_callbacks_see_output_and_tick(tmp_path):
    seen, ticks = bytearray(), []
    run._run_in_pty("echo hello; sleep 0.7; echo bye", tmp_path, {"PATH": "/usr/bin:/bin"}, [], 10,
                    on_data=seen.extend, on_tick=lambda: ticks.append(1))
    assert b"hello" in seen and b"bye" in seen
    assert ticks   # called while waiting, not just when output arrives


def test_dim_only_on_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(run.sys.stdout, "isatty", lambda: True)
    assert run._dim("$ ls") == "\x1b[2m$ ls\x1b[0m"
    monkeypatch.setenv("NO_COLOR", "1")
    assert run._dim("$ ls") == "$ ls"
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(run.sys.stdout, "isatty", lambda: False)
    assert run._dim("$ ls") == "$ ls"
