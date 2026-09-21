"""`zf channel status` — live channel discussion state at a glance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from zf.cli.main import main
from zf.core.events import EventWriter
from zf.core.events.log import EventLog


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {
        "version": "1.0",
        "project": {"name": "status-test", "state_dir": ".zf"},
        "roles": [{"name": "dev", "backend": "mock"}],
    }
    (tmp_path / "zf.yaml").write_text(yaml.dump(config))
    main(["init"])
    return tmp_path


def _seed_running_channel(state_dir: Path) -> None:
    writer = EventWriter(EventLog(state_dir / "events.jsonl"))
    writer.emit("channel.created", actor="test",
                payload={"channel_id": "ch-1", "name": "ch-1"})
    for member in ("arch", "critic"):
        writer.emit("channel.member.invited", actor="test",
                    payload={"channel_id": "ch-1", "member_id": member})
    writer.emit("channel.discussion.started", actor="test", payload={
        "channel_id": "ch-1",
        "thread_id": "main",
        "roster": ["arch", "critic"],
        "synthesizer": "critic",
        "requirement_message_id": "msg-1",
    })
    writer.emit("channel.agent.reply.requested", actor="test", payload={
        "channel_id": "ch-1",
        "thread_id": "main",
        "request_id": "reply-arch",
        "message_id": "msg-1",
        "target_member_id": "arch",
        "status": "started",
    })


def test_channel_status_shows_running_member(project: Path, capsys):
    _seed_running_channel(project / ".zf")
    rc = main(["channel", "status", "ch-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "channel: ch-1" in out
    assert "running: arch(started)" in out
    assert "roster:  2 members: arch, critic" in out


def test_channel_status_json_payload(project: Path, capsys):
    _seed_running_channel(project / ".zf")
    rc = main(["channel", "status", "ch-1", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"]
    assert payload["running"][0]["member_id"] == "arch"
    assert payload["roster"] == ["arch", "critic"]
    assert payload["questions"]["total"] == 0


def test_channel_status_unknown_channel_errors(project: Path, capsys):
    rc = main(["channel", "status", "ch-nope"])
    assert rc == 1
    assert "unknown channel" in capsys.readouterr().err
