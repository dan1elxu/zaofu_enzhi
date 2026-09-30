"""Hidden auto steps: bounded failure, durable recovery and controlled retry."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_channel_auto_mode import CH, _auto_channel, _open_owner_question, _writer
from zf.core.reactor.registry import EventActionRegistry
from zf.runtime import channel_auto_chain as chain, channel_auto_recovery as recovery
from zf.runtime.channel_conversation_projection import project_channel_conversation
from zf.runtime.channel_projection import project_channel
from zf.runtime.control_actions import ControlledActionService
from zf.runtime.orchestrator import _kernel_owns_liveness_event
from zf.runtime.tick_services import _sweep_channel_discussions
from zf.runtime.wake_patterns import WAKE_PATTERNS
from zf.web.headless_agent import HeadlessThreadStore, HeadlessTurnResult


@pytest.fixture
def setup_auto(tmp_path, monkeypatch):
    state_dir, writer = _writer(tmp_path)
    _auto_channel(writer, state_dir, tmp_path)
    _open_owner_question(writer)
    host = SimpleNamespace(state_dir=state_dir, event_writer=writer, config=None, project_root=tmp_path)
    jobs = []

    class Thread:
        def __init__(self, *, target, args, **kwargs):
            self.run = lambda: target(*args)
        def start(self):
            jobs.append(self.run)

    monkeypatch.setattr(recovery, 'threading', SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(sleep=lambda _: None))
    monkeypatch.setattr(chain, '_post_resolver_summary', lambda *args, **kwargs: None)
    recovery._IN_FLIGHT.clear()
    yield host, jobs
    recovery._IN_FLIGHT.clear()


def _result(**changes):
    values = dict(ok=False, status='failed', backend='codex-headless', thread_id='test',
                  provider_session_id='provider', reply='', messages=[], usage={}, error='Selected model is at capacity')
    values.update(changes)
    return HeadlessTurnResult(**values)


def _provider(monkeypatch, results):
    from zf.web import headless_agent
    run = Mock(side_effect=results) if isinstance(results, list) else Mock(return_value=results)
    monkeypatch.setattr(headless_agent.KanbanHeadlessAgent, 'run_turn', run)
    return run


def _step(host):
    return recovery.latest_step(project_channel(host.state_dir, CH) or {}, 'main', 'resolve')


def _start(host, jobs):
    registry = EventActionRegistry()
    chain.register_auto_chain_handlers(registry, host)
    event = host.event_writer.event_log.read_all()[-1]
    registry.resolve('channel.question.opened')[0].handler(event)
    assert len(jobs) == 1
    jobs.pop(0)()


def _success():
    return _result(ok=True, status='completed', error='', reply='{"resolutions": [{"question_id": "q-owner-1", "resolution": "answered", "answer": "Session cookie"}]}')


def test_provider_error_three_attempts_then_visible_failure(setup_auto, monkeypatch):
    host, jobs = setup_auto
    run = _provider(monkeypatch, _result())
    _start(host, jobs)
    assert run.call_count == 3
    step = _step(host)
    assert step['status'] == 'failed'
    assert step['attempt'] == step['max_attempts'] == 3
    assert 'Selected model is at capacity' in step['reason']
    assert not step['retryable']
    events = host.event_writer.event_log.read_all()
    assert [e.payload['attempt'] for e in events if e.type == recovery.STEP_EVENT and e.payload.get('status') == 'running'] == [1, 2, 3]
    assert len([e for e in events if e.type == 'channel.auto_chain.failed']) == 1
    assert project_channel_conversation(host.state_dir, CH)['auto_chain'][0]['status'] == 'failed'
    assert not recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    assert not jobs


@pytest.mark.parametrize('failure', [
    _result(status='timeout', error='no Codex app-server events for 300s'),
    _result(ok=True, status='completed', incomplete=True, completion_reason='stream interrupted', error=''),
])
def test_failed_or_incomplete_provider_response_is_never_parsed_as_success(setup_auto, monkeypatch, failure):
    host, jobs = setup_auto
    failure = replace(failure, reply=_success().reply)
    run = _provider(monkeypatch, failure)
    _start(host, jobs)
    assert run.call_count == 3
    assert _step(host)['status'] == 'failed'
    assert (failure.error or failure.completion_reason) in _step(host)['reason']


def test_failure_then_success_stops_retry_and_resolves_once(setup_auto, monkeypatch):
    host, jobs = setup_auto
    run = _provider(monkeypatch, [_result(), _success()])
    _start(host, jobs)
    assert run.call_count == 2
    assert _step(host)['status'] == 'completed'
    assert _step(host)['attempt'] == 2
    assert len([e for e in host.event_writer.event_log.read_all() if e.type == 'channel.question.resolved']) == 1
    assert not recovery.schedule_auto_step(host, CH, 'main', 'resolve')


def test_watch_restart_recovers_consumed_trigger_without_resetting_attempts(setup_auto, monkeypatch):
    host, jobs = setup_auto
    assert recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    step = _step(host)
    step['attempt'] = 2
    recovery._emit(host.event_writer, step, 'running')
    jobs.clear()  # Simulate process exit; the persisted event remains.
    recovery._IN_FLIGHT.clear()
    run = _provider(monkeypatch, _result(status='timeout', error='stream stalled'))
    monkeypatch.setattr('zf.runtime.channel_discussion.sweep_discussion_deadlines', lambda *args, **kwargs: 0)
    assert _sweep_channel_discussions(state_dir=host.state_dir, writer=host.event_writer, config=None, project_root=host.project_root) == 1
    jobs.pop(0)()
    assert run.call_count == 1
    assert _step(host)['attempt'] == 3
    assert _step(host)['status'] == 'failed'
    assert any(e.type == recovery.STEP_EVENT and e.payload.get('status') == 'interrupted' for e in host.event_writer.event_log.read_all())


def test_legacy_running_provider_is_interrupted_only_after_lock_released(setup_auto, monkeypatch):
    host, jobs = setup_auto
    store = HeadlessThreadStore(state_dir=host.state_dir, project_root=host.project_root)
    thread = store.load(thread_key=f'channel-auto-resolve:{CH}:main')
    store.pin_provider_session(thread, backend='codex-headless', provider_session_id='lost', workdir=str(host.project_root), status='running')
    run = _provider(monkeypatch, _result())
    with store.locked(thread['thread_id']):
        _start(host, jobs)
        assert run.call_count == 0
        assert store.load(thread_key=thread['thread_key'])['status'] == 'running'
    assert recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    jobs.pop(0)()
    assert run.call_count == 2  # Legacy lost attempt is attempt 1.
    assert store.load(thread_key=thread['thread_key'])['status'] == 'interrupted'
    assert _step(host)['status'] == 'failed'


def test_manual_retry_is_enqueued_and_consumed_by_registered_reactor(setup_auto, monkeypatch):
    host, jobs = setup_auto
    run = _provider(monkeypatch, _result())
    _start(host, jobs)
    failed = _step(host)
    service = ControlledActionService(host.state_dir, host.event_writer, project_root=host.project_root,
                                      actor='web', source='channel', surface='web')
    requested = host.event_writer.emit('web.action.requested', actor='web', payload={'action': 'channel-auto-retry'})
    payload = {key: failed[key] for key in ('channel_id', 'thread_id', 'purpose', 'operation_id')}
    result = service.execute(action='channel-auto-retry', requested_action='channel-auto-retry', payload=payload, requested=requested)
    assert result['ok'] and result['status'] == 'queued'
    assert run.call_count == 3 and not jobs  # HTTP/action does not launch providers.
    assert _step(host)['retry_of'] == failed['operation_id']
    duplicate = service.execute(action='channel-auto-retry', requested_action='channel-auto-retry', payload=payload, requested=requested)
    assert not duplicate['ok']
    run.return_value = _success()
    event = next(e for e in host.event_writer.event_log.read_all() if e.type == recovery.RETRY_EVENT)
    registry = EventActionRegistry()
    chain.register_auto_chain_handlers(registry, host)
    registry.resolve(recovery.RETRY_EVENT)[0].handler(event)
    jobs.pop(0)()
    assert _step(host)['status'] == 'completed'
    assert _step(host)['attempt'] == 1
    assert recovery.RETRY_EVENT in WAKE_PATTERNS
    assert _kernel_owns_liveness_event(None, recovery.RETRY_EVENT)


def test_same_step_is_not_started_twice(setup_auto):
    host, jobs = setup_auto
    assert recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    assert not recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    assert len(jobs) == 1
    recovery._IN_FLIGHT.clear()  # A second process is still fenced by the file lock.
    with recovery.step_lock(host.state_dir, CH, 'main', 'resolve'):
        assert not recovery.schedule_auto_step(host, CH, 'main', 'resolve')
    assert len(jobs) == 1


def test_web_retry_requires_token_and_returns_queued_projection(setup_auto, monkeypatch):
    from fastapi.testclient import TestClient
    from zf.web.server import create_app

    host, jobs = setup_auto
    run = _provider(monkeypatch, _result())
    _start(host, jobs)
    failed = _step(host)
    payload = {key: failed[key] for key in ('channel_id', 'thread_id', 'purpose', 'operation_id')}
    monkeypatch.setenv('ZF_WEB_ACTION_TOKEN', 'auto-test-token')
    client = TestClient(create_app(host.state_dir, project_root=host.project_root))
    assert client.post('/api/actions/channel-auto-retry', json=payload).status_code == 403
    response = client.post('/api/actions/channel-auto-retry', json=payload,
                           headers={'x-zf-web-token': 'auto-test-token'})
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'queued'
    assert run.call_count == 3 and not jobs
    assert _step(host)['status'] == 'queued'
    assert _step(host)['attempt'] == 0


def test_auto_disables_nested_transport_retry_but_other_callers_keep_it(tmp_path):
    from zf.web.headless_agent import KanbanHeadlessAgent

    run = Mock(return_value=_result(provider_session_id='', error='resume failed'))
    backend = SimpleNamespace(available=lambda: True, run_turn=run)
    agent = KanbanHeadlessAgent(state_dir=tmp_path / 'state', project_root=tmp_path,
                                backends={'codex-headless': backend}, runtime_logs_enabled=False)
    store = HeadlessThreadStore(state_dir=tmp_path / 'state', project_root=tmp_path)
    for key in ('auto', 'ordinary'):
        store.pin_provider_session(store.load(thread_key=key), backend='codex-headless',
                                   provider_session_id='old-session', workdir=str(tmp_path), status='idle')
    agent.run_turn(backend='codex-headless', message='test', thread_key='auto', retry_transport=False)
    assert run.call_count == 1
    run.reset_mock()
    agent.run_turn(backend='codex-headless', message='test', thread_key='ordinary')
    assert run.call_count == 2


def test_partial_resolution_keeps_failure_budget_across_ticks(setup_auto, monkeypatch):
    host, jobs = setup_auto
    host.event_writer.emit('channel.question.opened', actor='arch-1', correlation_id=CH,
        payload={'channel_id': CH, 'thread_id': 'main', 'question_id': 'q-owner-2',
                 'kind': 'owner_decision', 'question': 'Second question', 'target_member_id': 'owner',
                 'status': 'open', 'asked_by': 'arch-1', 'source': 'runtime'})
    run = _provider(monkeypatch, [_success(), _result(), _result()])
    _start(host, jobs)
    assert run.call_count == 3
    assert _step(host)['status'] == 'failed'
    assert not recovery.schedule_auto_step(host, CH, 'main', 'resolve')


def test_provider_exception_is_reported_after_bounded_retry(setup_auto, monkeypatch):
    host, jobs = setup_auto
    run = _provider(monkeypatch, [OSError('Too many open files')] * 3)
    _start(host, jobs)
    assert run.call_count == 3
    assert 'Too many open files' in _step(host)['reason']
    assert _step(host)['status'] == 'failed'


@pytest.mark.parametrize('readiness', ['ready', 'needs_multi_lens'])
def test_freeze_never_schedules_handoff_or_recovers_legacy_handoff(setup_auto, monkeypatch, readiness):
    from tests.test_channel_auto_mode import _ready_auto_prd

    host, jobs = setup_auto
    _ready_auto_prd(host.project_root, host.event_writer, host.state_dir, readiness_verdict=readiness)
    run = _provider(monkeypatch, _result())
    event = host.event_writer.event_log.read_all()[-1]
    registry = EventActionRegistry()
    chain.register_auto_chain_handlers(registry, host)
    assert not registry.resolve('channel.consensus.reached')
    step = {'channel_id': CH, 'thread_id': 'main', 'purpose': 'handoff',
            'source_id': event.id, 'cause_event_id': event.id, 'operation_id': 'legacy',
            'status': 'failed', 'attempt': 3, 'max_attempts': 3}
    host.event_writer.emit(recovery.STEP_EVENT, actor='test', payload=step)
    assert not recovery.schedule_auto_step(host, CH, 'main', 'handoff')
    assert not recovery._source(project_channel(host.state_dir, CH), 'main', 'resolve', require_ready=False)[0]
    service = ControlledActionService(host.state_dir, host.event_writer, project_root=host.project_root)
    requested = host.event_writer.emit('web.action.requested', actor='test', payload={})
    result = recovery.request_auto_retry(service, requested=requested, action='channel-auto-retry',
        requested_action='channel-auto-retry', payload=step)
    assert not result['ok']
    assert run.call_count == 0
    assert not jobs
    assert not [e for e in host.event_writer.event_log.read_all() if e.type in {
        'task.created', 'workflow.request.proposed', 'workflow.invoke.requested',
    }]
