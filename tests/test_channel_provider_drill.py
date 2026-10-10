"""The real Channel drill starts from initialized, isolated runtime truth."""

from fastapi.testclient import TestClient
import pytest

from tests.e2e.channel.channel_product_provider_drill import _prepare_state, run_drill
from zf.core.config.schema import ZfConfig
from zf.core.state.session import SessionStore
from zf.core.task.schema import Task
from zf.core.task.store import TaskStore
from zf.web.server import create_app


def test_drill_initialization_makes_the_actual_snapshot_caller_ready(tmp_path):
    project = tmp_path / "project"
    state = tmp_path / "external-state"
    _prepare_state(project, state)
    assert SessionStore(state / "session.yaml").load().project_root == str(project)
    assert TaskStore(state / "kanban.json").list_all() == []
    with TestClient(create_app(state, config=ZfConfig(), project_root=project)) as client:
        response = client.get("/api/snapshot/light")
    assert response.status_code == 200, response.text


def test_drill_refuses_reusing_state_without_overwriting_tasks(tmp_path):
    project, state = tmp_path / "project", tmp_path / "state"
    _prepare_state(project, state)
    TaskStore(state / "kanban.json").add(Task(id="kept", title="Existing task"))
    before = (state / "kanban.json").read_bytes()
    with pytest.raises(FileExistsError, match="fresh state"):
        run_drill(project_root=project, state_dir=state, backend="codex")
    assert (state / "kanban.json").read_bytes() == before
