"""The public Channel stream identity must interrupt its actual subprocess."""

import sys
import threading
import time

from fastapi.testclient import TestClient

from zf.core.events import EventLog, EventWriter
from zf.core.task.store import TaskStore
from zf.core.task.schema import Task
from zf.runtime.channel_adapter import _run_headless_reply
from zf.web.headless_agent import ClaudeHeadlessBackend
from zf.web.server import create_app


def test_channel_public_identity_cancels_actual_provider_process(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    TaskStore(state / "kanban.json").add(Task(id="TASK-TEST", title="isolated cancel"))
    writer = EventWriter(EventLog(state / "events.jsonl"))
    script = tmp_path / "provider.py"
    script.write_text(
        "import json,sys,time\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'system','session_id':'channel-session'}),flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'working'}]}}),flush=True)\n"
        "time.sleep(30)\n"
        "print(json.dumps({'type':'result','result':'too late'}),flush=True)\n"
    )
    backend = ClaudeHeadlessBackend(command=f"{sys.executable} {script}")
    result = {}

    def run():
        result["turn"] = _run_headless_reply(
            state_dir=state, project_root=tmp_path, writer=writer,
            channel={"channel_id": "ch-cancel", "project_id": "project-test"},
            member={"member_id": "arch", "backend": "claude", "permission_profile": "read_only"},
            message={"text": "Inspect the requirement"},
            request={"request_id": f"reply-{tmp_path.name}", "thread_id": "main", "target_member_id": "arch"},
            request_id=f"reply-{tmp_path.name}", started_event_id="started", actor="test", source="test",
            backend="claude-headless", backends={"claude-headless": backend},
        )

    monkeypatch.setenv("ZF_WEB_ACTION_TOKEN", "isolated-cancel-test")
    worker = threading.Thread(target=run)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            events = writer.event_log.read_all()
            if any(event.type == "agent.session.part.delta" for event in events):
                break
            time.sleep(0.02)
        public = next(event.payload for event in events if event.type == "agent.session.run.started")
        with TestClient(create_app(state, project_root=tmp_path)) as client:
            response = client.post("/api/actions/agent-session-cancel",
                headers={"x-zf-web-token": "isolated-cancel-test"}, json={
                    "run_id": public["run_id"], "thread_id": public["thread_id"],
                    "conversation_id": "ch-cancel", "project_id": "project-test",
                })
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["process_found"] and receipt["process_terminated"], receipt
        worker.join(5)
        assert not worker.is_alive()
        assert result["turn"].status == "cancelled"
        assert "too late" not in result["turn"].reply
    finally:
        if worker.is_alive():
            from zf.web.agent_session_runtime import cancel_agent_session_run, run_key
            if "public" in locals():
                cancel_agent_session_run(run_key(run_id=public["run_id"], thread_id=public["thread_id"]))
            worker.join(5)
