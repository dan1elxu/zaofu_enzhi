"""Codex 容量监控的行为回归；默认全部使用隔离的 tmux/ps runner。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_capacity_autocontinue.py"
SPEC = importlib.util.spec_from_file_location("codex_capacity_autocontinue", SCRIPT)
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)

ERROR = f"⚠ {M.CAPACITY_ERROR}"
SCREEN = f"{ERROR}\n\n› \n"
PLACEHOLDER = f"{ERROR}\n\n› \x1b[2mAsk Codex to do anything\x1b[0m\n"
PARTICLES = "\x1b[38;2;156;156;154m⠁\x1b[39m    \x1b[38;2;79;78;78m⡀\x1b[39m"
ANIMATED_SCREEN = (
    f"{ERROR}\n\n{PARTICLES}\n"
    f"› \x1b[2mAsk Codex to do anything\x1b[0m  {PARTICLES}\n"
)


def pane(screen=SCREEN, **changes):
    prompt_y = next((i for i, line in reversed(list(enumerate(screen.splitlines())))
                     if "›" in line), 0)
    result = M.Pane("zf", "0", "0", "%1", "123", "/dev/pts/1", 0, 0, 2, prompt_y, 0)
    return replace(result, **changes)


class FakeTmux:
    def __init__(self, screen=SCREEN):
        self.panes = {"%1": pane(screen)}
        self.screens = {"%1": screen}
        self.commands = []
        self.processes = f"{os.geteuid()} 200 200 node /usr/bin/codex\n"
        self.capture_error = False
        self.enter_error = False
        self.after_text = lambda: None
        self.socket_error = ""
        self.client = M.TmuxClient(runner=self, env={})

    def set_screen(self, screen, pane_id="%1"):
        previous = self.panes[pane_id]
        self.screens[pane_id] = screen
        self.panes[pane_id] = pane(screen, pane_id=pane_id, pid=previous.pid,
                                   session=previous.session)

    @staticmethod
    def row(p):
        return "\t".join(str(getattr(p, key)) for key in p.__dataclass_fields__)

    @property
    def sends(self):
        return [cmd for cmd in self.commands if cmd[1:2] == ["send-keys"]]

    def __call__(self, args, **kwargs):
        self.commands.append(args)
        stdout, stderr, status = "", "", 0
        if args[0] == "ps":
            stdout = self.processes
        else:
            assert args[0] == "tmux"
            command = args[1]
            pane_id = args[args.index("-t") + 1] if "-t" in args else None
            if command == "list-panes":
                selected = list(self.panes.values())
                if pane_id:
                    selected = [p for p in selected if p.session == pane_id.lstrip("=")]
                stdout = "\n".join(self.row(p) for p in selected)
                if self.socket_error:
                    status, stderr = 1, self.socket_error
            elif command == "display-message":
                stdout = self.row(self.panes[pane_id])
            elif command == "capture-pane":
                stdout = self.screens[pane_id]
                if self.capture_error:
                    status, stderr = 1, "capture failed"
            elif command == "send-keys":
                if args[-1] == "Enter" and self.enter_error:
                    status, stderr = 1, "send failed"
                if "-l" in args:
                    self.after_text()
            else:
                raise AssertionError(args)
        return subprocess.CompletedProcess(args, status, stdout=stdout, stderr=stderr)


def test_once_per_error_and_repeated_errors_with_old_error_visible(monkeypatch):
    fake = FakeTmux()
    now = [0.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: now[0])
    monitor = M.CapacityMonitor(fake.client, submit_delay=0)
    assert monitor.scan_once() == 1
    assert monitor.scan_once() == 0
    now[0] = 100
    assert monitor.scan_once() == 0  # 冷却过后仍不能重复回复同一条旧报错。

    # 下一次错误追加到屏幕上，第一条报错并未消失。
    fake.set_screen(f"{ERROR}\n› 继续\n{ERROR}\n\n› \n")
    assert monitor.scan_once() == 1
    assert monitor.scan_once() == 0
    assert [cmd[-1] for cmd in fake.sends] == ["继续", "Enter", "继续", "Enter"]


def test_new_error_waits_for_per_pane_cooldown(monkeypatch):
    fake = FakeTmux()
    now = [0]
    monkeypatch.setattr(M.time, "monotonic", lambda: now[0])
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=30)
    assert monitor.scan_once() == 1
    fake.set_screen(f"{ERROR}\n› 继续\n{ERROR}\n› \n")
    now[0] = 10
    assert monitor.scan_once() == 0
    now[0] = 30
    assert monitor.scan_once() == 1


def test_error_disappearing_and_reappearing_can_trigger_again():
    fake = FakeTmux()
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=0)
    assert monitor.scan_once() == 1
    fake.set_screen("• 工作完成\n› \n")
    assert monitor.scan_once() == 0
    fake.set_screen(SCREEN)
    assert monitor.scan_once() == 1


@pytest.mark.parametrize("screen", [
    f"› 用户引用：{M.CAPACITY_ERROR}\n• 工作完成\n› \n",
    f"• 日志包含 {M.CAPACITY_ERROR}\n› \n",
    f"{ERROR}\n› 继续\n• 已成功完成\n› \n",
    f"{ERROR}\n• Working (2s · esc to interrupt)\n› \n",
    f"{ERROR}\n› \nesc to interrupt",
    f"{ERROR}\n› 用户正在写的新需求\n",
    f"{ERROR}\n$ ",
    "⚠ Something else went wrong.\n› \n",
])
def test_does_not_reply_to_stale_busy_draft_or_unrelated_screens(screen):
    fake = FakeTmux(screen)
    assert M.CapacityMonitor(fake.client).scan_once() == 0
    assert fake.sends == []


@pytest.mark.parametrize("change", [{"dead": 1}, {"in_mode": 1}, {"cursor_x": 8}])
def test_skips_dead_copy_mode_and_nonempty_input(change):
    fake = FakeTmux()
    fake.panes["%1"] = replace(fake.panes["%1"], **change)
    assert M.CapacityMonitor(fake.client).scan_once() == 0
    assert fake.sends == []


@pytest.mark.parametrize("screen", [
    PLACEHOLDER,
    ANIMATED_SCREEN,
    "⚠ Selected model is at capa\ncity. Please try a different model.\n› \n",
    "\x1b[33m⚠ Selected model is at capacity.\x1b[0m\n  Please try a different model.\n› \n",
])
def test_supports_placeholder_ansi_and_narrow_panes(screen):
    fake = FakeTmux(screen)
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 1


def test_ignores_codex_background_terminal_status_line():
    fake = FakeTmux(
        f"{ERROR}\n"
        "1 background terminal running · /ps to view · /stop to close\n"
        "\n› \x1b[2mAsk Codex to do anything\x1b[0m\n"
    )
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 1


def test_animation_redraw_does_not_retry_an_old_error():
    fake = FakeTmux(ANIMATED_SCREEN)
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=0)
    assert monitor.scan_once() == 1
    next_frame = ANIMATED_SCREEN.replace("⠁", "⢀").replace("⡀", "⠈")
    fake.set_screen(next_frame)
    assert monitor.scan_once() == 0
    fake.set_screen(f"{ERROR}\n› 继续\n{next_frame}")
    assert monitor.scan_once() == 1
    assert [cmd[-1] for cmd in fake.sends] == ["继续", "Enter", "继续", "Enter"]


def test_animation_redraw_between_detection_and_send_is_allowed(monkeypatch):
    fake = FakeTmux(ANIMATED_SCREEN)
    frames = iter([ANIMATED_SCREEN, ANIMATED_SCREEN.replace("⠁", "⠄")])
    monkeypatch.setattr(fake.client, "capture", lambda pane_id: next(frames))
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 1
    assert [cmd[-1] for cmd in fake.sends] == ["继续", "Enter"]


@pytest.mark.parametrize("output", ["已完成", "⠁ 等待确认", "• 正在执行", "⠋"])
def test_animation_filter_preserves_real_output_and_busy_spinner(output):
    fake = FakeTmux(ANIMATED_SCREEN.replace(f"{ERROR}\n", f"{ERROR}\n{output}\n"))
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 0
    assert fake.sends == []


def test_draft_with_particles_and_cursor_at_start_is_preserved():
    fake = FakeTmux(ANIMATED_SCREEN.replace("\x1b[2mAsk Codex to do anything", "已有草稿"))
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 0
    assert fake.sends == []


@pytest.mark.parametrize("argv", [
    "/usr/bin/codex",
    "node /usr/bin/codex",
    "node /opt/lib/@openai/codex/bin/codex.js",
])
def test_native_codex_and_node_wrapper_are_recognized(argv):
    assert M.foreground_codex(f"1000 42 42 {argv}", 1000)


@pytest.mark.parametrize("processes", [
    "1001 42 42 codex",  # 其他用户
    "1000 42 43 codex",  # 后台或暂停的 Codex
    "1000 42 42 bash -c 'codex'",
    "1000 42 42 node /tmp/my-server.js --codex",
    "1000 42 42 echo codex",
    "malformed",
])
def test_process_filter_fails_closed(processes):
    assert not M.foreground_codex(processes, 1000)


def test_shell_is_not_sent_keys_even_with_stale_codex_screen():
    fake = FakeTmux()
    fake.processes = f"{os.geteuid()} 200 200 bash\n"
    assert M.CapacityMonitor(fake.client).scan_once() == 0
    assert fake.sends == []


def test_process_change_after_text_does_not_send_enter():
    fake = FakeTmux()
    fake.after_text = lambda: setattr(fake, "processes", f"{os.geteuid()} 300 300 bash\n")
    assert M.CapacityMonitor(fake.client, submit_delay=0).scan_once() == 0
    assert [cmd[-1] for cmd in fake.sends] == ["继续"]


def test_capture_error_does_not_rearm_old_error():
    fake = FakeTmux()
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=0)
    assert monitor.scan_once() == 1
    fake.capture_error = True
    assert monitor.scan_once() == 0
    fake.capture_error = False
    assert monitor.scan_once() == 0
    assert len(fake.sends) == 2


def test_partial_send_failure_is_not_repeated():
    fake = FakeTmux()
    fake.enter_error = True
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=0)
    assert monitor.scan_once() == 0
    assert monitor.scan_once() == 0
    assert len(fake.sends) == 2


def test_pane_replaced_and_removed_state_is_cleaned():
    fake = FakeTmux()
    monitor = M.CapacityMonitor(fake.client, submit_delay=0, cooldown=0)
    assert monitor.scan_once() == 1
    fake.panes["%1"] = replace(fake.panes["%1"], pid="456")
    assert monitor.scan_once() == 1
    assert len(monitor.states) == 1
    fake.panes = {}
    assert monitor.scan_once() == 0
    assert monitor.states == {}


def test_session_scope_and_independent_panes():
    fake = FakeTmux()
    fake.panes["%2"] = pane(pane_id="%2", session="zf-other", pid="456")
    fake.screens["%2"] = SCREEN
    monitor = M.CapacityMonitor(fake.client, session="zf", submit_delay=0)
    assert monitor.scan_once() == 1
    assert all(cmd[cmd.index("-t") + 1] == "%1" for cmd in fake.sends)
    listed = next(cmd for cmd in fake.commands if cmd[1] == "list-panes")
    assert "-a" not in listed and "=zf" in listed


def test_cli_dry_run_never_sends(monkeypatch, capsys):
    fake = FakeTmux()
    monkeypatch.setattr(M, "TmuxClient", lambda **kwargs: fake.client)
    assert M.main(["--once", "--dry-run"]) == 0
    assert fake.sends == []
    assert "dry-run" in capsys.readouterr().out


def test_empty_tmux_reports_zero_panes_but_permissions_fail():
    fake = FakeTmux()
    fake.socket_error = "error connecting to /tmp/tmux-test (No such file or directory)"
    assert fake.client.list_panes() == []
    fake.socket_error = "error connecting to /tmp/tmux-test (Operation not permitted)"
    with pytest.raises(RuntimeError, match="Operation not permitted"):
        fake.client.list_panes()


def test_socket_selection_honors_explicit_then_current_server():
    client = M.TmuxClient(env={"TMUX": "/tmp/custom,123,0", "TMUX_PANE": "%9"})
    assert client.socket == "/tmp/custom"
    assert "TMUX" not in client.env and "TMUX_PANE" not in client.env
    client = M.TmuxClient(socket="/tmp/explicit", env={"TMUX": "/tmp/custom,123,0"})
    assert client.socket == "/tmp/explicit"


@pytest.mark.parametrize("kwargs", [
    {"interval": 0}, {"interval": float("nan")}, {"cooldown": -1},
    {"cooldown": float("inf")}, {"submit_delay": -1}, {"reply": ""},
    {"reply": "继续\n/exit"}, {"reply": "\x1b"},
])
def test_invalid_configuration_is_rejected_before_tmux(kwargs):
    fake = FakeTmux()
    with pytest.raises(ValueError):
        M.CapacityMonitor(fake.client, **kwargs)
    assert fake.commands == []


@pytest.mark.host
@pytest.mark.parametrize("animated", [False, True])
def test_real_tmux_retries_twice_then_stops_at_success(animated):
    """真实 tmux + 本地模拟 CLI；独立 socket，不调用模型，不接触现有 panes。"""
    import shutil
    import tempfile
    import time
    from datetime import datetime, timezone

    if not shutil.which("tmux"):
        pytest.skip("tmux 未安装")
    prefix = "zf-codex-capacity-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
    with tempfile.TemporaryDirectory(prefix=prefix, dir="/tmp") as folder:
        root = Path(folder)
        mock = root / "mock.py"
        log = root / "replies.txt"
        particles = PARTICLES if animated else ""
        prompt = f"› \x1b[2mAsk Codex to do anything\x1b[0m{particles}\r\x1b[2C"
        mock.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            f"error = {ERROR!r}\n"
            "for attempt in range(2):\n"
            "    print(error)\n"
            f"    print({particles!r})\n"
            f"    print({prompt!r}, end='', flush=True)\n"
            "    reply = sys.stdin.readline().strip()\n"
            "    with Path(sys.argv[1]).open('a') as output:\n"
            "        output.write(reply + '\\n')\n"
            "print('• 已恢复\\n\\n› ', end='', flush=True)\n"
            "sys.stdin.readline()\n",
            encoding="utf-8",
        )
        # 模拟程序名为 codex，但实际执行 Python；没有真实 provider CLI。
        executable = root / "codex"
        executable.symlink_to(Path(sys.executable).resolve())
        client = M.TmuxClient(socket=str(root / "tmux.sock"), env=dict(os.environ))
        try:
            created = client.tmux([
                "-f", "/dev/null", "new-session", "-d", "-s", "capacity-test",
                "-x", "100", "-y", "40", str(executable), str(mock), str(log),
            ])
            assert created.returncode == 0, created.stderr
            monitor = M.CapacityMonitor(client, session="capacity-test", cooldown=0.2,
                                        interval=0.05, submit_delay=0.1)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                monitor.scan_once()
                if log.exists() and len(log.read_text().splitlines()) == 2:
                    break
                time.sleep(0.05)
            assert log.exists(), "mock Codex 没有收到输入"
            assert log.read_text().splitlines() == ["继续", "继续"]
            for _ in range(3):
                assert monitor.scan_once() == 0
            print("simulation.done: isolated tmux received two replies, then stopped")
        finally:
            client.tmux(["kill-session", "-t", "=capacity-test"])
