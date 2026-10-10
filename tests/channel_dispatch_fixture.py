"""Wait for real background dispatch threads in deterministic Channel tests."""

import pytest


@pytest.fixture
def channel_dispatches(monkeypatch):
    from zf.runtime import channel_message_ingress, control_actions_channel_msg, orchestrator_reactor

    dispatch = channel_message_ingress.dispatch_channel_replies_background
    threads = []

    def tracked(**kwargs):
        thread = dispatch(**kwargs)
        if thread is not None:
            threads.append(thread)
        return thread

    for module in (channel_message_ingress, control_actions_channel_msg, orchestrator_reactor):
        monkeypatch.setattr(module, "dispatch_channel_replies_background", tracked)

    def wait():
        while threads:
            thread = threads.pop(0)
            thread.join(timeout=5)
            assert not thread.is_alive(), "Channel dispatch did not finish"

    yield wait
    wait()
