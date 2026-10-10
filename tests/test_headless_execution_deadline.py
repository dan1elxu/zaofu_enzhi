"""Channel total budgets remain bounded even while native providers stream."""
import os
from pathlib import Path
import sys
import time

import pytest

from zf.web.headless_agent import ClaudeHeadlessBackend, CodexHeadlessBackend


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_native_stream_cannot_extend_explicit_execution_deadline(tmp_path: Path, provider: str):
    script = tmp_path / "stream.py"
    common = "import json, sys, time, os\nfrom pathlib import Path\nPath('provider.pid').write_text(str(os.getpid()))\n"
    if provider == "claude":
        body = """sys.stdin.readline()
print(json.dumps({'type':'system','session_id':'bounded-claude'}), flush=True)
for i in range(100):
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'partial '}]}}), flush=True)
    time.sleep(0.05)
"""
        backend = ClaudeHeadlessBackend(command=f"{sys.executable} {script}")
    else:
        body = """for line in sys.stdin:
    msg = json.loads(line)
    method, rid = msg.get('method'), msg.get('id')
    if not rid:
        continue
    result = {'threadId':'bounded-codex'} if method == 'thread/start' else {}
    print(json.dumps({'jsonrpc':'2.0','id':rid,'result':result}), flush=True)
    if method == 'turn/start':
        for i in range(100):
            print(json.dumps({'method':'item/agentMessage/delta','params':{'delta':'partial '}}), flush=True)
            time.sleep(0.05)
"""
        backend = CodexHeadlessBackend(command=f"{sys.executable} {script}")
    script.write_text(common + body)
    started = time.monotonic()
    result = backend.run_turn(
        prompt="bounded channel operation", cwd=tmp_path, system_prompt="",
        thread_id="bounded", provider_session_id="", on_session_id=lambda _: None,
        on_message=None, timeout_s=10, execution_deadline=started + 0.5,
    )
    assert time.monotonic() - started < 3
    assert not result.ok and result.status == "timeout"
    assert "total deadline" in result.error
    assert "partial" in result.reply
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "provider.pid").read_text()), 0)


def test_codex_rpc_setup_shares_the_total_deadline(tmp_path: Path):
    script = tmp_path / "no_rpc_response.py"
    script.write_text("import sys,time\nsys.stdin.readline()\ntime.sleep(10)\n")
    backend = CodexHeadlessBackend(command=f"{sys.executable} {script}")
    started = time.monotonic()
    result = backend.run_turn(
        prompt="setup must be bounded", cwd=tmp_path, system_prompt="",
        thread_id="setup", provider_session_id="", on_session_id=lambda _: None,
        on_message=None, timeout_s=10, execution_deadline=started + 0.3,
    )
    assert time.monotonic() - started < 3
    assert not result.ok and result.status == "timeout"
    assert "total deadline" in result.error
