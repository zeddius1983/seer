"""Tests for the SEER_TRACE event log used by the e2e scenarios."""

import json

from seer import trace


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_off_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SEER_TRACE", raising=False)
    trace.event("mode", mode="plain")
    assert not trace.enabled()
    assert list(tmp_path.iterdir()) == []


def test_events_are_json_lines(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SEER_TRACE", str(path))
    trace.event("mode", mode="brave", piped_chars=3)
    trace.event("answer", text="done — ✓")
    events = _events(path)
    assert [e["event"] for e in events] == ["mode", "answer"]
    assert events[0]["piped_chars"] == 3
    assert events[1]["text"] == "done — ✓"
    assert all("t" in e for e in events)


def test_unwritable_trace_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("SEER_TRACE", str(tmp_path / "missing-dir" / "trace.jsonl"))
    trace.event("mode", mode="plain")   # must not raise


def test_traced_provider_records_each_call(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SEER_TRACE", str(path))

    class Provider:
        resolved_model = "claude-sonnet-5-5"

        def stream(self, system, prompt):
            yield "hello "
            yield "world"

    provider = trace.TracedProvider(Provider())
    assert "".join(provider.stream("SYS", "PROMPT")) == "hello world"
    assert provider.resolved_model == "claude-sonnet-5-5"   # attributes pass through
    (call,) = _events(path)
    assert call["event"] == "llm"
    assert call["reply"] == "hello world"
    assert call["system_chars"] == 3 and call["prompt_chars"] == 6
    assert call["resolved_model"] == "claude-sonnet-5-5"


def test_traced_provider_records_a_stream_stopped_early(tmp_path, monkeypatch):
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("SEER_TRACE", str(path))

    class Provider:
        def stream(self, system, prompt):
            yield from ["a", "b", "c"]

    stream = trace.TracedProvider(Provider()).stream("S", "P")
    next(stream)
    stream.close()
    assert _events(path)[0]["reply"] == "a"
