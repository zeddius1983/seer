"""Tests for `seer --tui`: the launcher, the status bar and per-session memory."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time

import pytest
from click.testing import CliRunner

from seer import cli, conversation, tui
from seer.config import load_config


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    path = tmp_path / "seer" / "conversations"
    monkeypatch.setattr(conversation, "CONVERSATION_DIR", path)
    conversation._current_session_id.cache_clear()
    return path


@pytest.fixture
def in_session(monkeypatch):
    monkeypatch.setenv("SEER_TUI", "/tmp/tmux-1000/seer")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/seer,4242,7")
    monkeypatch.delenv("TMUX_PANE", raising=False)


class TestStatusBar:
    def test_shows_model_context_and_confirm_policy(self):
        text = tui.status_right("Sonnet 5.5", 3100, 200000, "auto", "nerdfont")
        assert "Sonnet 5.5" in text and "~3.1k/200.0k 2%" in text and "auto" in text

    def test_context_color_follows_usage(self):
        low = tui.status_right("m", 1000, 100000, "auto", "nerdfont")
        high = tui.status_right("m", 90000, 100000, "auto", "nerdfont")
        assert f"bg={tui._GREEN}" in low and f"bg={tui._RED}" in high

    def test_without_a_known_window_shows_only_the_estimate(self):
        text = tui.status_right("m", 3100, None, "auto", "nerdfont")
        assert "~3.1k " in text and "/" not in text.split("~3.1k")[1].split("#[")[0]

    def test_before_the_first_question_shows_no_usage(self):
        assert "~" not in tui.status_right("m", None, 200000, "auto", "nerdfont")

    def test_model_names_cannot_inject_tmux_formats(self):
        text = tui.status_right("evil#(rm -rf x)%H", 1, None, "auto", "nerdfont")
        assert "evil##(rm -rf x)%%H" in text

    def test_segments_join_with_the_neighbouring_background(self):
        glyphs = tui._GLYPHS["nerdfont"]
        text = tui._segments([("#111111", "a"), ("#222222", "b")], glyphs)
        assert f"#[fg=#111111,bg=#222222]{glyphs['sep']}" in text
        assert text.startswith(f"#[fg=#111111,bg=default]{glyphs['cap_left']}")
        assert f"#[fg=#222222,bg=default]{glyphs['cap_right']}" in text

    @pytest.mark.parametrize("build", [
        lambda: tui.status_right("m", 1000, 2000, "auto", "plain"),
        lambda: tui.status_left("plain"),
    ])
    def test_plain_style_needs_no_special_font(self, build):
        # Private-use codepoints are what render as boxes without a Nerd Font.
        assert not any(0xE000 <= ord(ch) <= 0xF8FF for ch in build())

    def test_left_side_quotes_the_pane_path_for_the_shell(self):
        assert "#{q:pane_current_path}" in tui.status_left("nerdfont")

    def test_push_targets_this_panes_session(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tui.subprocess, "run", lambda args, **kw: calls.append(args))
        monkeypatch.setenv("TMUX_PANE", "%3")
        tui.push_status("m", 1, None, "auto", "plain")
        assert calls[0][:5] == ["tmux", "set-option", "-t", "%3", "status-right"]

    def test_push_never_breaks_seer(self, monkeypatch):
        def missing(*args, **kw):
            raise FileNotFoundError("tmux")
        monkeypatch.setattr(tui.subprocess, "run", missing)
        tui.push_status("m", 1, None, "auto", "plain")


class _FakeProvider:
    resolved_model = "claude-sonnet-5-5"

    def __init__(self, fail=False):
        self.fail = fail

    def stream(self, system, prompt):
        yield "abcd"
        if self.fail:
            raise RuntimeError("boom")
        yield "efgh"


class TestMeteredProvider:
    def test_reports_request_and_reply(self):
        reports = []
        provider = tui.MeteredProvider(_FakeProvider(), reports.append)
        assert "".join(provider.stream("s" * 40, "p" * 40)) == "abcdefgh"
        assert reports == [22]   # 88 characters ÷ 4

    def test_reports_a_failed_call_too(self):
        reports = []
        provider = tui.MeteredProvider(_FakeProvider(fail=True), reports.append)
        with pytest.raises(RuntimeError):
            list(provider.stream("", ""))
        assert reports == [1]

    def test_counts_reasoning_the_provider_kept_to_itself(self):
        class Reasoning:
            reasoning = ""

            def stream(self, system, prompt):
                self.reasoning = "r" * 400
                yield "abcd"

        reports = []
        list(tui.MeteredProvider(Reasoning(), reports.append).stream("", ""))
        assert reports == [101]

    def test_status_line_still_sees_the_providers_attributes(self):
        assert tui.MeteredProvider(_FakeProvider(), print).resolved_model == "claude-sonnet-5-5"

    def test_only_used_in_a_session(self, monkeypatch, in_session):
        monkeypatch.setattr(cli, "get_provider", lambda pcfg: _FakeProvider())
        cfg, pcfg = load_config(), _pcfg()
        assert isinstance(cli._provider(cfg, pcfg), tui.MeteredProvider)
        monkeypatch.delenv("SEER_TUI")
        assert isinstance(cli._provider(cfg, pcfg), _FakeProvider)


def _pcfg():
    from seer.config import ProviderConfig
    return ProviderConfig(type="openai", model="m", context_window=1000)


class TestSessionName:
    def test_named_by_the_user(self):
        assert tui.session_name(["work", "api"]) == "work-api"

    def test_defaults_to_the_directory(self, tmp_path, monkeypatch):
        (tmp_path / "my.project").mkdir()
        monkeypatch.chdir(tmp_path / "my.project")
        assert tui.session_name([]) == "my-project"   # tmux doesn't allow dots

    def test_never_empty(self):
        assert tui.session_name(["..."]) == "seer"


class TestTmuxProblem:
    def _with_version(self, monkeypatch, version):
        monkeypatch.setattr(tui.shutil, "which", lambda name: "/usr/bin/tmux")
        monkeypatch.setattr(tui.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, version, ""))

    def test_missing(self, monkeypatch):
        monkeypatch.setattr(tui.shutil, "which", lambda name: None)
        assert "isn't installed" in tui.tmux_problem()

    def test_too_old(self, monkeypatch):
        self._with_version(monkeypatch, "tmux 2.9a\n")
        assert "3.2" in tui.tmux_problem()

    @pytest.mark.parametrize("version", ["tmux 3.2\n", "tmux 3.8\n", "tmux next-3.9\n", "tmux master\n"])
    def test_new_enough(self, monkeypatch, version):
        self._with_version(monkeypatch, version)
        assert tui.tmux_problem() is None


class TestCtrlGHint:
    def test_other_shells(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert "zsh" in tui._ctrl_g_hint()

    def test_zsh_without_the_binding(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        monkeypatch.setenv("ZDOTDIR", str(tmp_path))
        assert "installer" in tui._ctrl_g_hint()

    def test_zsh_with_the_binding(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        monkeypatch.setenv("ZDOTDIR", str(tmp_path))
        (tmp_path / ".zshrc").write_text("# -- seer-toolbox: seer-implicit --\n")
        assert tui._ctrl_g_hint() is None


class TestBraveDefaults:
    @pytest.fixture(autouse=True)
    def config(self, tmp_path, monkeypatch):
        self.path = tmp_path / "config.yaml"
        monkeypatch.setattr("seer.config.CONFIG_PATH", self.path)

    def test_brave_in_a_session(self, in_session):
        self.path.write_text("brave: false\n")
        assert load_config().brave is True

    def test_trust_asks_like_auto_in_a_session(self, in_session):
        self.path.write_text("brave_confirm: trust\n")
        assert load_config().brave_confirm == "auto"

    def test_always_stays_stricter_in_a_session(self, in_session):
        self.path.write_text("brave_confirm: always\n")
        assert load_config().brave_confirm == "always"

    def test_config_is_untouched_outside_a_session(self, monkeypatch):
        monkeypatch.delenv("SEER_TUI", raising=False)
        self.path.write_text("brave: false\nbrave_confirm: trust\n")
        cfg = load_config()
        assert cfg.brave is False and cfg.brave_confirm == "trust"

    def test_seer_tui_alone_is_not_a_session(self, monkeypatch):
        monkeypatch.setenv("SEER_TUI", "1")
        monkeypatch.delenv("TMUX", raising=False)
        assert load_config().brave is False

    def test_a_tmux_started_from_a_session_is_not_a_session(self, monkeypatch):
        monkeypatch.setenv("SEER_TUI", "/tmp/tmux-1000/seer")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,999,0")
        self.path.write_text("brave: false\n")
        assert load_config().brave is False

    def test_status_style_is_checked(self):
        self.path.write_text("tui_status: fancy\n")
        with pytest.raises(ValueError):
            load_config()


class TestSessionMemory:
    def test_panes_share_a_log_named_after_server_and_session(self, in_session):
        assert conversation._log_path().name == "tui-4242-7.json"

    def test_sessions_have_separate_memories(self, in_session, monkeypatch):
        conversation.record("first session", "answer")
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/seer,4242,8")
        assert conversation.earlier() is None

    def _pane_in_session(self, monkeypatch, result):
        monkeypatch.setenv("TMUX_PANE", "%3")
        monkeypatch.setattr(conversation.subprocess, "run", result)

    def test_uses_the_session_the_pane_is_in_now(self, in_session, monkeypatch):
        calls = []

        def tmux(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "$9\n", "")

        self._pane_in_session(monkeypatch, tmux)
        assert conversation._log_path().name == "tui-4242-9.json"
        assert conversation._log_path().name == "tui-4242-9.json"
        assert len(calls) == 1 and calls[0][-3:] == ["-t", "%3", "#{session_id}"]

    def test_falls_back_to_where_the_shell_started_if_tmux_fails(self, in_session, monkeypatch):
        self._pane_in_session(monkeypatch, lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "no pane"))
        assert conversation._log_path().name == "tui-4242-7.json"

    def test_falls_back_if_tmux_cannot_run(self, in_session, monkeypatch):
        def missing(*args, **kw):
            raise FileNotFoundError("tmux")
        self._pane_in_session(monkeypatch, missing)
        assert conversation._log_path().name == "tui-4242-7.json"

    def test_exchanges_do_not_expire_while_the_session_is_open(self, in_session):
        conversation.record("old question", "answer")
        log = conversation._log_path()
        exchanges = json.loads(log.read_text())
        exchanges[0]["time"] = time.time() - 7 * 24 * 60 * 60
        log.write_text(json.dumps(exchanges))
        conversation.record("new question", "answer")
        text = conversation.earlier()
        assert "old question" in text and "new question" in text

    def test_keeps_far_more_than_a_terminal(self, in_session):
        for n in range(conversation.MAX_EXCHANGES + 3):
            conversation.record(f"question {n}", "answer")
        assert "question 0" in conversation.earlier()

    def test_stops_at_the_session_limit(self, in_session):
        for n in range(conversation.SESSION_MAX_EXCHANGES + 1):
            conversation.record(f"question-{n}-", "answer")
        text = conversation.earlier()
        assert "question-0-" not in text and "question-1-" in text

    def test_what_is_sent_stays_within_budget_newest_first(self, in_session):
        for n in range(conversation.SESSION_MAX_EXCHANGES):
            conversation.record(f"question-{n}-", "x" * conversation.MAX_ANSWER_CHARS)
        text = conversation.earlier()
        assert "question-19-" in text and "question-0-" not in text
        assert len(text) <= conversation.SESSION_MAX_CHARS

    def test_budget_counts_directories_and_framing(self, in_session):
        for n in range(conversation.SESSION_MAX_EXCHANGES):
            conversation.record(f"question-{n}-", "x" * conversation.MAX_ANSWER_CHARS)
        log = conversation._log_path()
        exchanges = json.loads(log.read_text())
        for e in exchanges:
            e["cwd"] = "/deep" * 200
        log.write_text(json.dumps(exchanges))
        assert len(conversation.earlier()) <= conversation.SESSION_MAX_CHARS

    def test_one_huge_exchange_is_still_kept(self):
        huge = {"cwd": "/", "question": "q", "answer": "x" * 50000, "commands": []}
        assert conversation._fit_budget([huge]) == [huge]

    def test_survives_an_idle_half_hour(self, in_session, monkeypatch, log_dir):
        conversation.record("question", "answer")
        hour_ago = time.time() - 3600
        os.utime(conversation._log_path(), (hour_ago, hour_ago))
        monkeypatch.delenv("SEER_TUI")   # another terminal's run prunes the directory
        conversation.record("elsewhere", "answer")
        assert (log_dir / "tui-4242-7.json").exists()

    def _idle_log_then_prune(self, monkeypatch, server_running):
        conversation.record("question", "answer")
        old = time.time() - conversation.ABANDONED_LOG_AGE - 1
        os.utime(conversation._log_path(), (old, old))
        monkeypatch.setattr(conversation, "_server_running", lambda pid: server_running)
        monkeypatch.delenv("SEER_TUI")
        conversation.record("elsewhere", "answer")

    def test_an_open_session_survives_days_idle(self, in_session, monkeypatch, log_dir):
        self._idle_log_then_prune(monkeypatch, server_running=True)
        assert (log_dir / "tui-4242-7.json").exists()

    def test_a_log_is_pruned_once_its_server_is_gone(self, in_session, monkeypatch, log_dir):
        self._idle_log_then_prune(monkeypatch, server_running=False)
        assert not (log_dir / "tui-4242-7.json").exists()

    def test_an_unrecognisable_session_log_counts_as_abandoned(self, log_dir):
        assert conversation._is_abandoned(log_dir / "tui-oops.json")

    def test_server_running_checks_the_process(self):
        assert conversation._server_running(os.getpid())
        finished = subprocess.Popen([sys.executable, "-c", ""])
        finished.wait()
        assert not conversation._server_running(finished.pid)
        assert not conversation._server_running(2 ** 70)

    def test_terminals_outside_a_session_are_unchanged(self, monkeypatch):
        monkeypatch.delenv("SEER_TUI", raising=False)
        assert not conversation._log_path().name.startswith("tui-")
        assert "30 min" in conversation.describe_limits()


class TestCommand:
    def _run(self, *args):
        return CliRunner().invoke(cli.main, ["--tui", *args])

    def test_model_overrides_are_refused_rather_than_ignored(self):
        result = self._run("-m", "x")
        assert result.exit_code == 1 and "-p or -m" in result.output

    def test_unknown_options_are_not_session_names(self, monkeypatch):
        assert self._run("--wat").exit_code == 1

    def test_explains_when_tmux_is_missing(self, monkeypatch):
        monkeypatch.setattr(tui.shutil, "which", lambda name: None)
        result = self._run()
        assert result.exit_code == 1 and "isn't installed" in result.output

    def test_needs_a_terminal(self, monkeypatch):
        monkeypatch.setattr(tui, "tmux_problem", lambda: None)
        result = self._run()   # CliRunner's stdin and stdout aren't terminals
        assert result.exit_code == 1 and "needs a terminal" in result.output

    def test_closed_hook_keeps_the_formats_intact(self):
        # A Path would collapse the // and tmux would reject the substitution.
        assert "#{s/[$]//:hook_session}" in tui._closed_hook()

    def test_closed_hook_quotes_the_path_for_the_shell(self, monkeypatch, tmp_path):
        monkeypatch.setattr(conversation, "CONVERSATION_DIR", tmp_path / "cache dir")
        inner = shlex.split(tui._closed_hook())[1]   # as tmux hands it to the shell
        assert shlex.split(inner)[:2] == ["rm", "-f"] and len(shlex.split(inner)) == 3


def _status_script(path, *glyphs):
    return subprocess.run(
        ["sh", str(tui.STATUS_SCRIPT), str(path), *(glyphs or (">", ")", "", ""))],
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
class TestStatusScript:
    def _repo(self, path):
        def git(*args):
            subprocess.run(
                ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                cwd=path, check=True, capture_output=True,
            )
        git("init", "-b", "main")
        (path / "tracked").write_text("a")
        git("add", "tracked")
        git("commit", "-m", "x")
        return path

    def test_shortens_the_path_like_the_prompt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Developer" / "seer").mkdir(parents=True)
        assert "~/D/seer" in _status_script(tmp_path / "Developer" / "seer")

    def test_outside_a_repository_there_is_no_git_segment(self, tmp_path):
        out = _status_script(tmp_path)
        assert out.count(">") == 1 and out.endswith(")#[default]")

    def test_clean_repository(self, tmp_path):
        out = _status_script(self._repo(tmp_path))
        assert "main " in out and "bg=#689d6a" in out and "●" not in out

    def test_changes_turn_it_orange_and_are_counted(self, tmp_path):
        self._repo(tmp_path)
        (tmp_path / "tracked").write_text("b")
        (tmp_path / "new").write_text("c")
        out = _status_script(tmp_path)
        assert "main ●1 ?1" in out and "bg=#d65d0e] main" in out

    def test_untracked_alone_is_not_dirty(self, tmp_path):
        self._repo(tmp_path)
        (tmp_path / "new").write_text("c")
        out = _status_script(tmp_path)
        assert "?1" in out and "bg=#689d6a] main" in out

    def test_a_hash_in_a_path_cannot_start_a_tmux_style(self, tmp_path):
        weird = tmp_path / "a#[bg=red]"
        weird.mkdir()
        assert "a##[bg=red]" in _status_script(weird)


@pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")
class TestRealTmux:
    """A throwaway server, so the user's own sessions are never touched."""

    @pytest.fixture(autouse=True)
    def server(self, tmp_path, monkeypatch):
        # One each: a killed server can still be exiting when the next test starts.
        self.socket = f"seer-test-{os.getpid()}-{tmp_path.name}"
        self.tmp = tmp_path
        monkeypatch.setattr(tui, "SOCKET", self.socket)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setenv("SHELL", "/bin/sh")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in sys.path if p))
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.delenv("SEER_TUI", raising=False)
        yield
        subprocess.run(["tmux", "-L", self.socket, "kill-server"], capture_output=True)

    def _tmux(self, *args):
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True)

    def _create(self, name):
        tui.create_session(name, "Sonnet 5.5", 200000, "auto", "nerdfont")

    def _in_pane(self, target, code, out):
        """Run Python in a pane, as a seer run there would. Returns what it wrote."""
        path = self.tmp / out
        command = f"{sys.executable} -c 'import sys; {code}; open(sys.argv[1], \"w\").write(str(r))' {path}"
        self._tmux("send-keys", "-t", target, command, "Enter")
        deadline = time.time() + 20
        while time.time() < deadline:
            if path.exists() and path.read_text():
                return path.read_text()
            time.sleep(0.1)
        pytest.fail(f"pane {target} did not run: {self._tmux('capture-pane', '-p', '-t', target).stdout}")

    def _log_name(self, target, out):
        return self._in_pane(target, "from seer import conversation as c; r = c._log_path().name", out)

    def test_panes_share_memory_and_sessions_do_not(self):
        self._create("one")
        self._create("two")
        self._tmux("split-window", "-t", "=one:")
        pane_a = self._log_name("=one:.0", "a")
        pane_b = self._log_name("=one:.1", "b")
        other = self._log_name("=two:", "c")
        assert pane_a == pane_b and pane_a != other and pane_a.startswith("tui-")

    def test_session_gets_brave_defaults_from_the_server(self):
        self._create("one")
        assert self._in_pane(
            "=one:", "from seer.config import load_config; r = load_config().brave", "brave",
        ) == "True"

    def test_creating_twice_keeps_the_session_and_its_status(self):
        self._create("one")
        self._tmux("set-option", "-t", "=one:", "status-right", "kept")
        self._create("one")
        assert self._tmux("list-sessions", "-F", "#S").stdout.split() == ["one"]
        assert "kept" in self._tmux("show-options", "-t", "=one:", "status-right").stdout

    def test_status_is_per_session(self):
        self._create("one")
        self._create("two")
        self._in_pane(
            "=one:",
            'from seer import tui; tui.push_status("Other Model", 5, None, "auto", "plain"); r = 1',
            "pushed",
        )
        assert "Other Model" in self._tmux("show-options", "-t", "=one:", "status-right").stdout
        assert "Other Model" not in self._tmux("show-options", "-t", "=two:", "status-right").stdout

    def test_closing_a_session_deletes_only_its_memory(self, log_dir):
        self._create("one")
        self._create("two")
        first = self._log_name("=one:", "a")
        second = self._log_name("=two:", "b")
        log_dir.mkdir(parents=True)
        (log_dir / first).touch()
        (log_dir / second).touch()
        self._tmux("kill-session", "-t", "=one")
        deadline = time.time() + 5
        while (log_dir / first).exists() and time.time() < deadline:
            time.sleep(0.1)
        assert not (log_dir / first).exists() and (log_dir / second).exists()

    @pytest.mark.parametrize("dirname", ["cache dir", "it's here", "semi;colon"])
    def test_closing_a_session_handles_any_cache_path(self, monkeypatch, dirname):
        log_dir = self.tmp / dirname
        log_dir.mkdir()
        monkeypatch.setattr(conversation, "CONVERSATION_DIR", log_dir)
        self._create("keep")
        self._create("one")
        log = log_dir / self._log_name("=one:", "a")
        log.touch()
        self._tmux("kill-session", "-t", "=one")
        deadline = time.time() + 5
        while log.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert not log.exists()

    def test_a_tmux_started_from_a_pane_is_not_a_session(self):
        self._create("one")
        other = f"{self.socket}-other"
        out = self.tmp / "inner"
        probe = self.tmp / "probe.py"
        probe.write_text(
            "from seer.config import in_tui, load_config\n"
            f"open({str(out)!r}, 'w').write(str((in_tui(), load_config().brave)))\n"
        )
        command = f"tmux -L {other} -f /dev/null new-session -d -s plain '{sys.executable} {probe}'"
        self._tmux("send-keys", "-t", "=one:", command, "Enter")
        try:
            deadline = time.time() + 20
            while time.time() < deadline and not (out.exists() and out.read_text()):
                time.sleep(0.1)
            assert out.read_text() == "(False, False)"
        finally:
            subprocess.run(["tmux", "-L", other, "kill-server"], capture_output=True)

    def test_switching_moves_only_the_terminal_that_asked(self):
        for name in ("asking", "other", "target"):
            self._create(name)
        outer = f"{self.socket}-outer"
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        try:
            for name in ("asking", "other"):
                subprocess.run(
                    ["tmux", "-L", outer, "-f", "/dev/null", "new-session", "-d", "-s", name,
                     f"tmux -L {self.socket} attach-session -t ={name}"],
                    env=env, check=True,
                )
                time.sleep(0.5)
            # A key typed into "other" makes it the terminal tmux picks if it has to.
            subprocess.run(["tmux", "-L", outer, "send-keys", "-t", "=other:", "Enter"], check=True)
            time.sleep(0.5)
            code =f'from seer import tui; tui.SOCKET = "{self.socket}"; r = tui.attach_session("target")'
            assert self._in_pane("=asking:", code, "switched") == "0"
            time.sleep(0.5)
            clients = self._tmux("list-clients", "-F", "#{client_session}").stdout.split()
            assert sorted(clients) == ["other", "target"]
        finally:
            subprocess.run(["tmux", "-L", outer, "kill-server"], capture_output=True)

    def test_status_left_runs_the_script(self):
        self._create("one")
        left = self._tmux("show-options", "-t", "=one:", "status-left").stdout
        assert "seer-status.sh" in left

    def test_a_new_session_keeps_the_look_of_existing_ones(self):
        def shown(session):   # -A: what the session really uses, inherited or not
            return self._tmux("show-options", "-A", "-t", f"={session}:", "status-left").stdout

        tui.create_session("fancy", "m", None, "auto", "nerdfont")
        tui.create_session("simple", "m", None, "auto", "plain")
        assert "\ue0b0" in shown("fancy") and "\ue0b0" not in shown("simple")

    def test_a_moved_window_joins_its_new_session(self, log_dir):
        self._create("from")
        self._create("to")
        self._tmux("new-window", "-d", "-t", "=from:")
        started = self._log_name("=from:1", "a")
        joined = self._log_name("=to:", "b")
        assert started != joined
        self._tmux("move-window", "-s", "=from:1", "-t", "=to:")
        assert self._log_name("=to:1", "c") == joined

    def test_launching_at_the_same_moment_joins_one_session(self):
        errors = []

        def launch():
            try:
                self._create("race")
            except subprocess.CalledProcessError as e:
                errors.append(e.stderr)

        threads = [threading.Thread(target=launch) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert self._tmux("list-sessions", "-F", "#S").stdout.split() == ["race"]

    def test_other_tmux_errors_are_still_reported(self, monkeypatch):
        monkeypatch.setattr(tui.subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "boom"))
        with pytest.raises(subprocess.CalledProcessError):
            self._create("one")

    def _write_tmux_conf(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("set-option -ga @loaded x\n")

    def _times_loaded(self):
        self._create("one")
        return len(self._tmux("show-options", "-gqv", "@loaded").stdout.strip())

    def test_loads_tmux_config_from_a_custom_xdg_directory(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.tmp / "xdg"))
        self._write_tmux_conf(self.tmp / "xdg" / "tmux" / "tmux.conf")
        assert self._times_loaded() == 1

    @pytest.mark.parametrize("xdg", [None, ".config"])
    def test_loads_tmux_config_from_the_usual_directory_once(self, monkeypatch, xdg):
        if xdg:
            monkeypatch.setenv("XDG_CONFIG_HOME", str(self.tmp / xdg))
        else:
            monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        self._write_tmux_conf(self.tmp / ".config" / "tmux" / "tmux.conf")
        assert self._times_loaded() == 1

    def test_loads_tmux_config_from_home(self):
        self._write_tmux_conf(self.tmp / ".tmux.conf")
        assert self._times_loaded() == 1
