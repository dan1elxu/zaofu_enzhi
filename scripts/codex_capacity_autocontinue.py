#!/usr/bin/env python3
"""tmux 中 Codex 容量报错的自动继续工具（Linux / Python 3.11+）。

用法及边界见 docs/impl/codex-capacity-autocontinue.md。
只操作经前台进程确认的 Codex 输入框，不读写 ZaoFu runtime state。
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

CAPACITY_ERROR = "Selected model is at capacity. Please try a different model."
BACKGROUND_TERMINAL_STATUS_RE = re.compile(
    r"^\s*\d+ background terminal running · /ps to view · /stop to close\s*$"
)
DEFAULT_REPLY = "继续"
ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*?(?:\x07|\x1b\\))")
Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class Pane:
    session: str
    window: str
    index: str
    pane_id: str
    pid: str
    tty: str
    dead: int
    in_mode: int
    cursor_x: int
    cursor_y: int
    history_size: int

    @property
    def identity(self) -> tuple[str, str]:
        return self.pane_id, self.pid

    @property
    def label(self) -> str:
        return f"{self.session}:{self.window}.{self.index} ({self.pane_id})"


def compact(text: str) -> str:
    # tmux 窄窗格可能在单词中间换行；忽略空白仍要求完整固定文案。
    return "".join(ANSI_RE.sub("", text).split())


def error_fingerprint(screen: str, pane: Pane) -> str | None:
    """只识别输入框前最新的完整警告；历史报错或正在输入时不触发。"""
    if pane.dead or pane.in_mode:
        return None
    if any(marker in screen.lower() for marker in ("esc to interrupt", "esc to cancel")):
        return None
    raw_lines = screen.splitlines()
    lines = [ANSI_RE.sub("", line).rstrip() for line in raw_lines]
    if not 0 <= pane.cursor_y < len(lines):
        return None
    prompt = lines[pane.cursor_y]
    if not prompt.startswith("›") or pane.cursor_x != 2:
        return None
    draft = prompt[1:].strip()
    if draft:
        # Codex 的空输入框显示灰色/暗色 placeholder，光标仍在列 2。
        # 普通草稿（包括把光标移到开头的草稿）必须保留。
        raw_tail = raw_lines[pane.cursor_y].split("›", 1)[1]
        codes = re.findall(r"\x1b\[([0-9;]*)m", raw_tail)
        if not any(code in {"2", "90", "2;90", "38;5;8", "38;5;244"} for code in codes):
            return None
    body = lines[:pane.cursor_y]
    warnings = [i for i, line in enumerate(body) if line.lstrip().startswith("⚠")]
    if not warnings:
        return None
    start = warnings[-1]
    # 输入框上方会绘制单点粒子动画；只忽略纯装饰行，保留真实输出和忙碌 spinner。
    warning_lines = [
        line for line in body[start:]
        if line.strip(" \t─━⠁⠂⠄⠈⠐⠠⡀⢀")
        and not BACKGROUND_TERMINAL_STATUS_RE.fullmatch(line)
    ]
    warning = "\n".join(warning_lines).lstrip().removeprefix("⚠").lstrip("\ufe0f ")
    if compact(warning) != compact(CAPACITY_ERROR):
        return None
    # 位置 + 上文让旧警告仍在屏幕上的下一轮警告也可重试。
    # 排除警告后的动画，避免重绘阻止发送前复核或把同一警告当作新报错。
    occurrence = f"{pane.history_size + start}:" + compact("\n".join(body[:start] + warning_lines))
    return hashlib.sha256(occurrence.encode()).hexdigest()


def foreground_codex(processes: str, uid: int) -> bool:
    """从同一 tty 的 ps 结果确认当前用户的前台 Codex（含 Node wrapper）。"""
    for line in processes.splitlines():
        fields = line.split(None, 3)
        if len(fields) != 4 or fields[0] != str(uid) or fields[1] != fields[2]:
            continue
        if not fields[1].isdigit() or int(fields[1]) <= 0:
            continue
        try:
            argv = shlex.split(fields[3])
        except ValueError:
            continue
        if not argv:
            continue
        exe = Path(argv[0]).name
        if exe == "codex":
            return True
        if exe in {"node", "nodejs"} and len(argv) > 1:
            if Path(argv[1]).name in {"codex", "codex.js", "codex.mjs"}:
                return True
    return False


class TmuxClient:
    FORMAT = "\t".join("#{" + field + "}" for field in (
        "session_name", "window_index", "pane_index", "pane_id", "pane_pid",
        "pane_tty", "pane_dead", "pane_in_mode", "cursor_x", "cursor_y", "history_size",
    ))

    def __init__(self, *, socket: str | None = None, runner: Runner | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        inherited = self.env.pop("TMUX", "").split(",", 1)[0]
        self.env.pop("TMUX_PANE", None)
        self.socket = socket or inherited
        self.runner = runner or subprocess.run

    def command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(args, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", check=False, timeout=5, env=self.env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"{args[0]} 调用失败：{exc}") from exc

    def tmux(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self.command(["tmux", *(["-S", self.socket] if self.socket else []), *args])

    @staticmethod
    def parse_pane(row: str) -> Pane:
        fields = row.split("\t")
        if len(fields) != 11 or not re.fullmatch(r"%\d+", fields[3]):
            raise RuntimeError("无法识别 tmux pane 信息")
        try:
            return Pane(*fields[:6], *(int(item) for item in fields[6:]))
        except ValueError as exc:
            raise RuntimeError("tmux pane 数值字段无效") from exc

    def list_panes(self, session: str | None = None) -> list[Pane]:
        # -a 会忽略 -t；限定 session 使用 -s 和精确 session 名。
        scope = ["-s", "-t", f"={session}"] if session else ["-a"]
        result = self.tmux(["list-panes", *scope, "-F", self.FORMAT])
        if result.returncode:
            detail = result.stderr.strip()
            if "no server running" in detail or (
                "error connecting to" in detail and
                any(reason in detail for reason in ("No such file or directory", "Connection refused"))
            ):
                return []
            raise RuntimeError(detail or "tmux list-panes 失败")
        return [self.parse_pane(row) for row in result.stdout.splitlines()]

    def pane(self, pane_id: str) -> Pane:
        result = self.tmux(["display-message", "-p", "-t", pane_id, self.FORMAT])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"无法读取 {pane_id}")
        return self.parse_pane(result.stdout.strip("\n"))

    def is_codex(self, pane: Pane) -> bool:
        if pane.dead or pane.in_mode or not pane.tty.startswith("/dev/"):
            return False
        result = self.command([
            "ps", "-t", pane.tty.removeprefix("/dev/"), "-o", "uid=,pgid=,tpgid=,args=",
        ])
        return result.returncode == 0 and foreground_codex(result.stdout, os.geteuid())

    def capture(self, pane_id: str) -> str:
        # 保留颜色以分辨 placeholder 与草稿；只读当前屏幕，不读历史滚屏。
        result = self.tmux(["capture-pane", "-p", "-e", "-t", pane_id])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"无法截取 {pane_id}")
        return result.stdout

    def send_reply(self, pane: Pane, reply: str, fingerprint: str, *, delay: float) -> bool:
        current = self.pane(pane.pane_id)
        if current.identity != pane.identity or not self.is_codex(current):
            return False
        if error_fingerprint(self.capture(pane.pane_id), current) != fingerprint:
            return False
        text = self.tmux(["send-keys", "-t", pane.pane_id, "-l", "--", reply])
        if text.returncode:
            raise RuntimeError(text.stderr.strip() or "发送文字失败")
        time.sleep(delay)
        current = self.pane(pane.pane_id)
        if current.identity != pane.identity or not self.is_codex(current):
            raise RuntimeError("发送文字后前台进程已变化，未发送 Enter")
        enter = self.tmux(["send-keys", "-t", pane.pane_id, "Enter"])
        if enter.returncode:
            raise RuntimeError(enter.stderr.strip() or "发送 Enter 失败，请检查 pane 草稿")
        return True


@dataclass
class RetryState:
    fingerprint: str = ""
    last_sent: float = float("-inf")


class CapacityMonitor:
    def __init__(self, client: TmuxClient, *, reply: str = DEFAULT_REPLY,
                 interval: float = 2.0, cooldown: float = 30.0,
                 submit_delay: float = 0.5, session: str | None = None,
                 dry_run: bool = False) -> None:
        if not reply.strip() or any(ord(c) < 32 or ord(c) == 127 for c in reply):
            raise ValueError("reply 必须是非空单行文字，不能含控制字符")
        for name, value in (("interval", interval), ("cooldown", cooldown), ("submit_delay", submit_delay)):
            if not math.isfinite(value) or value < 0 or (name == "interval" and value == 0):
                raise ValueError(f"{name} 数值无效")
        self.client, self.reply, self.session = client, reply, session
        self.interval, self.cooldown, self.submit_delay = interval, cooldown, submit_delay
        self.dry_run = dry_run
        self.states: dict[tuple[str, str], RetryState] = {}

    def scan_once(self) -> int:
        panes = self.client.list_panes(self.session)
        live = {pane.identity for pane in panes}
        self.states = {key: state for key, state in self.states.items() if key in live}
        sent = 0
        for pane in panes:
            try:
                if not self.client.is_codex(pane):
                    continue
                screen = self.client.capture(pane.pane_id)
                state = self.states.setdefault(pane.identity, RetryState())
                if compact(CAPACITY_ERROR) not in compact(screen):
                    state.fingerprint = ""
                fingerprint = error_fingerprint(screen, pane)
                if fingerprint is None or fingerprint == state.fingerprint:
                    continue
                now = time.monotonic()
                if now - state.last_sent < self.cooldown:
                    continue
                # 先记录尝试，避免文字发出后 Enter 失败导致每轮追加“继续”。
                state.fingerprint, state.last_sent = fingerprint, now
                if not self.dry_run and not self.client.send_reply(
                    pane, self.reply, fingerprint, delay=self.submit_delay,
                ):
                    state.fingerprint = ""
                    continue
                action = "检测到报错（dry-run）" if self.dry_run else f"已发送 {self.reply!r} + Enter"
                print(f"[{time.strftime('%H:%M:%S')}] {pane.label}: {action}", flush=True)
                sent += 1
            except RuntimeError as exc:
                print(f"[错误] {pane.label}: {exc}", file=sys.stderr, flush=True)
        return sent

    def run(self, *, once: bool = False) -> int:
        print(f"监控 tmux 中的 Codex；间隔 {self.interval:g}s，冷却 {self.cooldown:g}s；Ctrl-C 停止。", flush=True)
        try:
            while True:
                self.scan_once()
                if once:
                    return 0
                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\n已停止监控。", flush=True)
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监控全部 tmux 窗格中的 Codex，容量报错后自动发送“继续”。")
    parser.add_argument("--session", help="只监控指定 session（精确名称）")
    parser.add_argument("--socket", help="tmux socket 路径；默认当前 TMUX server 或当前用户的默认 server")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数（默认 2）")
    parser.add_argument("--cooldown", type=float, default=30.0, help="每个 pane 两次重试的最小间隔秒数（默认 30）")
    parser.add_argument("--submit-delay", type=float, default=0.5, help="发送文字到 Enter 的等待秒数（默认 0.5）")
    parser.add_argument("--reply", default=DEFAULT_REPLY, help="单行回复文字（默认：继续）")
    parser.add_argument("--dry-run", action="store_true", help="只检测，不发送按键")
    parser.add_argument("--once", action="store_true", help="只扫描一轮")
    args = parser.parse_args(argv)
    try:
        monitor = CapacityMonitor(TmuxClient(socket=args.socket), reply=args.reply,
                                  interval=args.interval, cooldown=args.cooldown,
                                  submit_delay=args.submit_delay, session=args.session,
                                  dry_run=args.dry_run)
        return monitor.run(once=args.once)
    except (RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
