"""Slot isolation at the tmux boundary; never starts a real provider."""
import json
import shlex
from types import SimpleNamespace

import pytest

from zf.runtime import evaluation_tmux as tmux, evaluation_runner as runner
from zf.runtime.evaluation_turns import JudgeTurnObservation


def test_codex_launch_sets_home_on_provider_and_targets_only_its_slot(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(tmux, "ensure_judges_window", lambda s, root, **kw: seen.append(kw) or True)
    monkeypatch.setattr(tmux, "stop_judge_agent", lambda s, **kw: seen.append(kw))
    monkeypatch.setattr(tmux, "_reset_judges_pane", lambda s, **kw: seen.append(kw))
    monkeypatch.setattr(tmux, "_send_line", lambda s, window, command, **kw: seen.append((window, command)))
    monkeypatch.setattr(tmux, "wait_judge_ready", lambda s, **kw: (True, ""))
    monkeypatch.setattr(tmux, "build_judge_argv", lambda **kw: ["codex", "--test"])
    home = tmp_path / "isolated home"
    assert tmux.start_judge_agent("session", tmp_path, backend="codex", session_uuid="uuid",
                                  system_prompt="", slot_id=1, codex_home=home) == (True, "")
    assert all(row["window_name"] == "judges-slot-1" for row in seen[:-1])
    window, command = seen[-1]
    assert window == "judges-slot-1"
    assert shlex.split(command) == ["cd", str(tmp_path), "&&", "env", f"CODEX_HOME={home}", "codex", "--test"]


def test_stop_and_large_paste_do_not_touch_other_slots(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_pane_provider_alive", lambda s, backend, **kw: True)
    monkeypatch.setattr(tmux.time, "sleep", lambda delay: None)
    monkeypatch.setattr(tmux, "_wait_pane_quiet", lambda *a, **kw: None)
    monkeypatch.setattr(tmux.subprocess, "run", lambda args, **kwargs: calls.append(args))
    tmux.stop_judge_agent("session", backend="codex", slot_id=1)
    assert calls and all("session:judges-slot-1" in args for args in calls)
    assert not any("pkill" in args or "/exit" in args for args in calls)
    calls.clear()
    for slot in (0, 1):
        tmux.send_prompt_to_window("session", "x" * 9000, slot_id=slot)
    buffers = [args[args.index("-b") + 1] for args in calls if "load-buffer" in args]
    assert len(set(buffers)) == 2
    for slot, buffer in enumerate(buffers):
        paste = next(args for args in calls if "paste-buffer" in args and buffer in args)
        assert f"session:judges-slot-{slot}" in paste


def test_provider_launches_bind_distinct_sessions_and_cleanup_failed_slot(tmp_path, monkeypatch):
    starts, stops = [], []
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "source-home"))
    monkeypatch.setattr(tmux, "start_judge_agent", lambda *a, **kw: starts.append(kw) or (True, ""))
    monkeypatch.setattr(tmux, "stop_judge_agent", lambda *a, **kw: stops.append(kw))
    monkeypatch.setattr(tmux, "send_prompt_to_window", lambda *a, **kw: None)
    def read(root, launch, **kwargs):
        observed = JudgeTurnObservation(status="aborted", error="interrupted", turn_id=f"turn-{launch}",
                                        session_id=f"native-{launch}", transcript_ref=f"/rollouts/{launch}")
        kwargs["on_observation"](observed)
        return observed
    monkeypatch.setattr(tmux, "read_judge_reply", read)
    config = SimpleNamespace(session=SimpleNamespace(tmux_session="s"), orchestrator=SimpleNamespace(backend="codex"))
    for slot in (0, 1):
        runner._visible_tmux_backend(state_dir=tmp_path, project_root=tmp_path, config=config,
                                     request_id=f"request-{slot}", slot_id=slot, prompt="review")
    assert starts[0]["session_uuid"] != starts[1]["session_uuid"]
    assert starts[0]["codex_home"] != starts[1]["codex_home"]
    assert [call["slot_id"] for call in stops] == [0, 1]
    for slot in (0, 1):
        binding = json.loads(next((tmp_path / "artifacts/benchmark-evaluations/sessions" / f"slot-{slot}").glob("*/binding.json")).read_text())
        assert binding["request_id"] == f"request-{slot}"
        assert binding["turn_id"].endswith(binding["session_uuid"])
        assert binding["session_id"].startswith("native-")
