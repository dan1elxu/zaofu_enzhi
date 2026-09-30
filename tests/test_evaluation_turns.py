"""Provider liveness is bound to one request/session/turn, without real CLIs."""
import json
from pathlib import Path

import pytest

from zf.runtime import evaluation_turns as turns


def write_records(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return path


def codex_start(turn: str = "current", marker: str = "evaluation-turn:request") -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "session"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": marker}},
    ]


@pytest.mark.parametrize("record,status", [
    ({"type": "event_msg", "payload": {"type": "task_complete", "error": "at capacity"}}, "provider_error"),
    ({"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "interrupted"}}, "aborted"),
    ({"type": "response_item", "payload": {"type": "error", "message": "tool failed"}}, "provider_error"),
])
def test_codex_current_turn_errors_ignore_past_turn(tmp_path, record, status):
    path = write_records(tmp_path / "rollout.jsonl", [
        *codex_start("old", "earlier"),
        {"type": "event_msg", "payload": {"type": "task_complete", "error": "old error"}},
        *codex_start(), record,
    ])
    observed = turns.observe_transcript(path, backend="codex", request_marker="evaluation-turn:request")
    assert observed.status == status
    assert observed.turn_id == "current"
    assert observed.error != "old error"
    # The next turn can fail without changing the completed selected turn.
    write_records(path, [*codex_start(),
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "correct answer"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        *codex_start("later", "later"), record,
    ])
    observed = turns.observe_transcript(path, backend="codex", current_turn_id="current")
    assert observed.status == "completed" and observed.reply == "correct answer"


@pytest.mark.parametrize("failure,status", [
    ({"type": "error", "error": "capacity"}, "provider_error"),
    ({"type": "assistant", "message": {"is_error": True}}, "provider_error"),
    ({"type": "assistant", "isApiErrorMessage": True}, "provider_error"),
    ({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "failed"}]}}, "provider_error"),
    ({"type": "assistant", "message": {"stop_reason": "aborted"}}, "aborted"),
])
def test_claude_errors_and_tool_results_are_scoped(tmp_path, failure, status):
    path = write_records(tmp_path / "claude.jsonl", [
        {"type": "user", "uuid": "old", "message": {"content": "earlier"}}, failure,
        {"type": "user", "uuid": "new", "sessionId": "session", "message": {"content": "request-marker"}},
    ])
    assert turns.observe_transcript(path, backend="claude-code", request_marker="request-marker").status == "running"
    with path.open("a") as handle:
        handle.write(json.dumps(failure) + "\n")
    observed = turns.observe_transcript(path, backend="claude-code", request_marker="request-marker")
    assert observed.status == status and observed.turn_id == "new"


def test_claude_returns_only_assistant_answer(tmp_path):
    path = write_records(tmp_path / "claude.jsonl", [
        {"type": "user", "uuid": "new", "message": {"content": "marker"}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "wrong answer"}]}},
        {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "answer"}]}},
    ])
    assert turns.observe_transcript(path, backend="claude-code", request_marker="marker").reply == "answer"


def test_transcript_model_is_observed_from_provider_records(tmp_path):
    codex = write_records(tmp_path / "codex.jsonl", [
        *codex_start(),
        {"type": "turn_context", "payload": {"turn_id": "current", "model": "gpt-codex"}},
    ])
    observed = turns.observe_transcript(codex, backend="codex", request_marker="evaluation-turn:request")
    assert observed.observed_model == "gpt-codex"
    assert observed.observed_model_source == "transcript.turn_context"

    claude = write_records(tmp_path / "claude-model.jsonl", [
        {"type": "user", "uuid": "new", "message": {"content": "marker"}},
        {"type": "assistant", "message": {"model": "claude-test", "stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}},
    ])
    observed = turns.observe_transcript(claude, backend="claude-code", request_marker="marker")
    assert observed.observed_model == "claude-test"
    assert observed.observed_model_source == "transcript.assistant.message"


def test_transcript_model_missing_is_unknown(tmp_path):
    path = write_records(tmp_path / "claude-unknown.jsonl", [
        {"type": "user", "uuid": "new", "message": {"content": "marker"}},
        {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]}},
    ])
    observed = turns.observe_transcript(path, backend="claude-code", request_marker="marker")
    assert observed.observed_model == "unknown"


def test_codex_binds_isolated_rollout_by_request_not_newest(tmp_path):
    root = tmp_path / "sessions"
    wanted = write_records(root / "2026/09/23/rollout-wanted.jsonl", codex_start())
    write_records(root / "2026/09/23/rollout-newest.jsonl", codex_start("other", "another request"))
    assert turns._find_rollout(root, "evaluation-turn:request", None) == wanted
    assert turns.read_judge_reply(tmp_path, "uuid", backend="codex", timeout_s=0).status == "provider_error"
    write_records(root / "2026/09/23/rollout-ambiguous.jsonl", codex_start())
    with pytest.raises(ValueError, match="ambiguous"):
        turns._find_rollout(root, "evaluation-turn:request", None)


def monitor(tmp_path, monkeypatch, records, **budgets):
    path = write_records(tmp_path / "sessions/2026/09/23/rollout-current.jsonl", records)
    clock = [0.0]
    monkeypatch.setattr(turns.time, "monotonic", lambda: clock[0])
    def sleep(seconds):
        clock[0] += seconds
        # Metadata continues to grow, but cannot renew effective progress.
        with path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "count": clock[0]}}) + "\n")
    monkeypatch.setattr(turns.time, "sleep", sleep)
    for suffix in ("FIRST_PROGRESS_TIMEOUT_S", "IDLE_TIMEOUT_S", "MAX_TOOL_CALLS", "MAX_SAME_TOOL_FINGERPRINT", "MAX_TOOL_WALL_TIME_S"):
        monkeypatch.setenv(f"ZF_EVALUATION_JUDGE_{suffix}", str(budgets.get(suffix, 0)))
    observed = turns.read_judge_reply(
        tmp_path, "uuid", backend="codex", timeout_s=10, poll_s=1,
        codex_sessions_root=tmp_path / "sessions", request_marker="evaluation-turn:request",
    )
    return observed, clock[0]


def test_no_effective_progress_times_out_despite_growing_metadata(tmp_path, monkeypatch):
    observed, elapsed = monitor(tmp_path, monkeypatch, codex_start(), FIRST_PROGRESS_TIMEOUT_S=3)
    assert observed.status == "timeout" and "no effective" in observed.error
    assert elapsed == 3


def test_idle_timeout_is_current_turn_progress_not_file_growth(tmp_path, monkeypatch):
    observed, elapsed = monitor(tmp_path, monkeypatch, [*codex_start(),
        {"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "thinking"}},
    ], IDLE_TIMEOUT_S=4)
    assert "idle" in observed.error and elapsed == 4


@pytest.mark.parametrize("limit", ["MAX_TOOL_CALLS", "MAX_SAME_TOOL_FINGERPRINT"])
def test_runaway_budgets_are_explicit(tmp_path, monkeypatch, limit):
    calls = [{"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
              "arguments": "python heredoc", "call_id": str(index)}} for index in range(3)]
    observed, elapsed = monitor(tmp_path, monkeypatch, [*codex_start(), *calls], **{limit: 2})
    assert observed.status == "timeout" and limit.lower() in observed.error
    assert observed.tool_calls == 3 and elapsed == 0


def test_tool_wall_and_total_budgets(tmp_path, monkeypatch):
    observed, elapsed = monitor(tmp_path, monkeypatch, [*codex_start(),
        {"type": "response_item", "timestamp": "2020-01-01T00:00:00Z",
         "payload": {"type": "function_call", "call_id": "long-tool", "name": "exec_command"}},
    ], MAX_TOOL_WALL_TIME_S=100)
    assert "max_tool_wall_time" in observed.error and elapsed == 0
    observed, elapsed = monitor(tmp_path, monkeypatch, codex_start())
    assert "timed out after 10s" in observed.error and elapsed == 10
