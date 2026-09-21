"""tmux window hosting for the PRD evaluation observer and its Judges.

``zf start`` opens one extra window named ``evaluation`` in the project
workflow session running ``zf evaluation-watch``. The window is outside the
role layout registry: it is plain infrastructure (like the run-manager
resident), it dies with the session on ``zf stop``, and respawning is
idempotent so repeated ``zf start`` runs never stack duplicate windows.

Judge runs are deliberately NOT one transient window per request. All judge
turns execute sequentially in a single persistent ``judges`` window in the
same session — that window is the visible "judge page". The agent is
spawned the same way the orchestrator spawns its workers: argv built by
the configured project's provider adapter with ``permission_mode="bypass"``
(for example ``--dangerously-skip-permissions`` for Claude), the evaluator
persona, TUI readiness proven by the adapter's ready pattern before the
packet is pasted (two-phase send-keys), and the provider transcript polled
for the final reply. Between judges the previous provider exits; the pane
returns to a shell ready for the next turn.

Historical note (2026-09-16): the first implementation waited for the
provider transcript file BEFORE pasting the prompt. Interactive claude
creates that file only when the first prompt is submitted, so every judge
died with "judge claude did not start". Readiness is pane-based now; the
transcript only proves the answer.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

EVALUATION_WINDOW_NAME = "evaluation"
JUDGES_WINDOW_NAME = "judges"
JUDGE_ROLE_NAME = "judge"
_EVALUATION_SESSION_MARKER = "evaluation.tmux.json"

_READY_POLL_S = 0.5
_SHELL_NAMES = {"bash", "sh", "zsh", "fish"}


def evaluation_window_enabled(config: Any | None) -> bool:
    """Env kill switch; default on so benchmark runs evaluate out of the box."""

    raw = os.environ.get("ZF_EVALUATION_WINDOW", "1").strip().lower()
    return raw not in {"0", "false", "off", "no"}


def _tmux(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _window_exists(tmux_session: str, window_name: str) -> bool:
    result = _tmux(["list-windows", "-t", tmux_session, "-F", "#{window_name}"])
    if result.returncode != 0:
        return False
    return window_name in {
        line.strip() for line in result.stdout.splitlines() if line.strip()
    }


def _spawn_window(tmux_session: str, window_name: str, project_root: Path) -> bool:
    result = _tmux([
        "new-window", "-t", tmux_session, "-n", window_name,
        "-c", str(project_root),
    ])
    return result.returncode == 0


def spawn_evaluation_window(
    *,
    tmux_session: str,
    state_dir: Path,
    project_root: Path,
    dry_run: bool = False,
) -> bool:
    """Open the ``evaluation`` tmux window once; return True when running."""

    if not tmux_session:
        return False
    if _window_exists(tmux_session, EVALUATION_WINDOW_NAME):
        return True
    if dry_run:
        return True
    from zf.runtime.cli_command import zf_cli_cmd

    command = (
        f"cd {shlex.quote(str(project_root))} && "
        f"ZF_STATE_DIR={shlex.quote(str(state_dir))} "
        f"{zf_cli_cmd()} evaluation-watch"
    )
    result = _tmux([
        "new-window", "-t", tmux_session, "-n", EVALUATION_WINDOW_NAME,
        "-c", str(project_root), command,
    ])
    return result.returncode == 0


# ------------------------------------------------------------- judge page


def judge_session_uuid(
    project_root: Path,
    workflow_run_id: str,
    judge_type: str,
    request_id: str,
) -> str:
    """Deterministic request id; Claude uses it as its session id.

    Codex creates its own rollout id on first interactive spawn, so the
    runner uses this value only as the request identity and finds the new
    project-matched rollout by its creation time.
    """

    import uuid

    stable = (
        f"zaofu-evaluation-judge:{Path(project_root).resolve()}:"
        f"{workflow_run_id}:{judge_type}:{request_id}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, stable))


def _interactive_backend(value: object) -> str:
    """Map a configured provider id to an interactive Judge adapter."""

    from zf.core.config.backend_identity import canonical_backend_id

    backend = canonical_backend_id(value)
    return {
        "claude-headless": "claude-code",
        "codex-headless": "codex",
    }.get(backend, backend)


def resolve_judge_backend(config: Any | None) -> str:
    """Follow the project's main agent provider for visible Judge turns.

    ``orchestrator.backend`` is the source of truth.  ``python``/``mock``
    are kernel adapters rather than provider CLIs, so a project using one of
    those routes falls back to its first configured Claude/Codex role.  The
    final Claude fallback preserves the historical behavior for minimal
    configs that do not declare a provider role.
    """

    candidates: list[object] = []
    orchestrator = getattr(config, "orchestrator", None)
    if orchestrator is not None:
        candidates.append(getattr(orchestrator, "backend", ""))
    for role in getattr(config, "roles", ()) or ():
        candidates.append(getattr(role, "backend", ""))
    for candidate in candidates:
        backend = _interactive_backend(candidate)
        if backend in {"claude-code", "codex"}:
            return backend
    return "claude-code"


def build_judge_role(backend: str = "claude-code") -> Any:
    """Judge agent role, spawned exactly like an orchestrator bypass worker."""

    from zf.core.config.schema import RoleConfig

    backend = _interactive_backend(backend)
    if backend not in {"claude-code", "codex"}:
        backend = "claude-code"
    return RoleConfig(
        name=JUDGE_ROLE_NAME,
        backend=backend,
        permission_mode="bypass",
    )


def build_judge_argv(
    *, session_uuid: str, system_prompt: str, backend: str = "claude-code",
) -> list[str]:
    """Orchestrator-parity argv: adapter-built bypass command + judge persona.

    The selected provider adapter owns its flag contract.  Claude supports
    ``--append-system-prompt``; Codex receives the same persona inline with
    the packet because its interactive CLI has no equivalent flag.
    """

    from zf.runtime.backend import get_adapter

    role = build_judge_role(backend)
    argv = get_adapter(role.backend).build_command(
        role,
        session_id=session_uuid,
        is_resume=False,
    )
    if role.backend == "claude-code":
        argv.extend(["--append-system-prompt", system_prompt])
    return argv


def ensure_judges_window(tmux_session: str, project_root: Path) -> bool:
    """Create the persistent ``judges`` window (shell) once; return presence."""

    if not tmux_session:
        return False
    if _window_exists(tmux_session, JUDGES_WINDOW_NAME):
        return True
    return _spawn_window(tmux_session, JUDGES_WINDOW_NAME, project_root)


def _pane_current_command(tmux_session: str, window_name: str) -> str:
    result = _tmux([
        "display-message", "-p",
        "-t", f"{tmux_session}:{window_name}", "#{pane_current_command}",
    ])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _pane_process_tree_has_executable(
    tmux_session: str, window_name: str, executable: str,
) -> bool:
    """True when any process at or below the pane pid runs ``executable``.

    Script-based provider CLIs (codex runs under node) show the interpreter
    as ``#{pane_current_command}``, so liveness cannot rely on the leaf name
    alone — probe the process tree exactly like TmuxTransport does.
    """

    pid_result = _tmux([
        "display-message", "-p",
        "-t", f"{tmux_session}:{window_name}", "#{pane_pid}",
    ])
    pane_pid = pid_result.stdout.strip()
    if pid_result.returncode != 0 or not pane_pid.isdigit():
        return False
    ps = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,command="],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if ps.returncode != 0:
        return False
    children: dict[str, list[str]] = {}
    commands: dict[str, str] = {}
    for raw in ps.stdout.splitlines():
        parts = raw.strip().split(None, 2)
        if len(parts) < 3:
            continue
        child, parent, command = parts
        children.setdefault(parent, []).append(child)
        commands[child] = command
    stack = [pane_pid]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        command = commands.get(current, "")
        tokens = command.split()
        for token in tokens:
            if token.rsplit("/", 1)[-1].lower() == executable.lower():
                return True
        stack.extend(children.get(current, []))
    return False


def _pane_provider_alive(tmux_session: str, backend: str) -> bool:
    """Judge-provider liveness for ``wait_judge_ready``/``stop_judge_agent``.

    Mirrors TmuxTransport._agent_process_liveness: the pane leaf matches the
    provider binary directly, except codex which runs under node (the pane
    leaf is ``node`` and the codex wrapper only shows up in the process
    tree). Without the probe, a codex judge pane never reads as alive and
    every judge turn dies with provider_ready_pattern_timeout
    (2026-09-18 test_dev5: all three v4 judges timed out this way).
    """

    backend = _interactive_backend(backend)
    provider_command = "codex" if backend == "codex" else "claude"
    leaf = _pane_current_command(tmux_session, JUDGES_WINDOW_NAME).rsplit("/", 1)[-1]
    if leaf == provider_command:
        return True
    if backend == "codex" and leaf == "node":
        return _pane_process_tree_has_executable(
            tmux_session, JUDGES_WINDOW_NAME, "codex",
        )
    return False


def _capture_pane(tmux_session: str, window_name: str, *, lines: int = 60) -> str:
    result = _tmux([
        "capture-pane", "-p", "-S", str(-lines),
        "-t", f"{tmux_session}:{window_name}",
    ])
    if result.returncode != 0:
        return ""
    return result.stdout


def _wait_pane_quiet(
    tmux_session: str,
    window_name: str,
    *,
    min_s: float = 0.5,
    max_s: float = 15.0,
) -> None:
    """Wait until the pane stops changing (a big paste has landed).

    A ~90 KiB paste-buffer streams into the TUI input for seconds; a fixed
    0.5 s settle lets the trailing Enter land mid-paste and get swallowed
    as draft text (2026-09-17 test_dev3: prompt sat unsubmitted, no
    transcript was ever created). Poll capture stability instead.
    """

    time.sleep(max(min_s, 0.0))
    deadline = time.monotonic() + max_s
    previous: str | None = None
    while time.monotonic() < deadline:
        current = _capture_pane(tmux_session, window_name, lines=4000)
        if current == previous:
            return
        previous = current
        time.sleep(0.7)


def _send_line(
    tmux_session: str,
    window_name: str,
    text: str,
    *,
    backend: str = "claude-code",
) -> None:
    """Deliver text to the TUI and submit with Enter.

    Two delivery modes:

    * **Typed** (``send-keys -l``, payloads up to ~8 KiB): keystrokes are
      never folded. Codex's TUI replaces ANY ``paste-buffer`` payload with a
      literal ``[Pasted Content N chars]`` placeholder — observed at 1 KiB,
      and the placeholder replaces the real input in the model context.
      Judge prompts are short file references, so they always take this path.
    * **Paste** (``paste-buffer``, oversized fallback): streams via stdin so
      size is not a factor (tmux argv caps ~16 KiB — 2026-09-17 test_dev3
      "command too long" incident). Codex folds these; large payloads should
      instead use the packet-file reference (``_write_judge_prompt_sidecar``).

    Enter is a separate call after a per-backend settle grace: codex's
    ratatui input handler loses an Enter arriving back-to-back with input
    (B-1203-06 R-1, tmux.send_keys), and pane-quiet alone is not sufficient
    because the screen settles before the input buffer does.
    """

    target = f"{tmux_session}:{window_name}"
    text = text.rstrip("\n")
    settle_s = 1.5 if _interactive_backend(backend) == "codex" else 0.0
    if len(text.encode("utf-8")) <= 8000:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-l", text], check=True,
        )
        if settle_s > 0:
            time.sleep(settle_s)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)
        return
    subprocess.run(
        ["tmux", "load-buffer", "-b", "zf-judge-paste", "-"],
        input=text, text=True, check=True,
    )
    try:
        subprocess.run(
            ["tmux", "paste-buffer", "-b", "zf-judge-paste", "-t", target],
            check=True,
        )
        _wait_pane_quiet(tmux_session, window_name)
        if settle_s > 0:
            time.sleep(settle_s)
        subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)
    finally:
        subprocess.run(
            ["tmux", "delete-buffer", "-b", "zf-judge-paste"], check=False,
        )


def stop_judge_agent(
    tmux_session: str,
    *,
    backend: str = "claude-code",
    timeout_s: float = 10.0,
) -> None:
    """Terminate a live Judge provider; guarantee a clean shell afterwards.

    The previous implementation raced the next spawn: after ``/exit`` it
    waited for the pane to leave ``claude`` and gave up with a bare C-c,
    but a exiting claude can still hold its session lock for seconds, and
    a C-c only interrupts generation — the process survives. The next
    ``start_judge_agent`` then launched a new claude with the SAME
    deterministic session id and claude refused with
    "Session ID ... is already in use" (2026-09-17 test_dev3, recurring).
    Escalation order: C-c (interrupt generation) -> /exit (graceful) ->
    respawn-pane -k (kill the whole process group, clear queued input).
    """

    backend = _interactive_backend(backend)
    provider_command = "codex" if backend == "codex" else "claude"
    if not _pane_provider_alive(tmux_session, backend):
        return
    target = f"{tmux_session}:{JUDGES_WINDOW_NAME}"
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "C-c"], check=False,
    )
    time.sleep(_READY_POLL_S)
    # Codex has no /exit slash-command: pasted text lands in its input box
    # as a chat message (the judge pane once accumulated several "cd ... &&
    # codex ..." lines answered with "session already running"). Interrupt
    # and go straight to the hard pane reset. The reset is unconditional
    # for codex (not only after a 10 s grace): a prompt pasted into a dead
    # provider's shell can leave bash inside an unclosed-quote PS2
    # continuation, and the next spawn's launch command would then be
    # swallowed as continuation text (2026-09-18 test_dev5 retry loop).
    if backend == "codex":
        _reset_judges_pane(tmux_session)
        return
    _send_line(tmux_session, JUDGES_WINDOW_NAME, "/exit")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pane_provider_alive(tmux_session, backend):
            return
        time.sleep(_READY_POLL_S)
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "C-c"], check=False,
    )
    time.sleep(1.0)
    if (
        _pane_current_command(tmux_session, JUDGES_WINDOW_NAME).rsplit("/", 1)[-1]
        != provider_command
    ):
        return
    _reset_judges_pane(tmux_session)


def _reset_judges_pane(tmux_session: str) -> None:
    """Hard-reset the judges pane: kill everything inside, clear queued input."""

    subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", f"{tmux_session}:{JUDGES_WINDOW_NAME}"],
        check=False,
    )
    time.sleep(_READY_POLL_S)


def ensure_workspace_trusted(cwd: Path) -> None:
    """Pre-accept the Claude workspace trust dialog for ``cwd``.

    Same contract as ``SpawnCoordinator._ensure_claude_workspace_trusted``:
    a pane that hits the trust dialog would swallow the pasted packet.
    Best-effort; any I/O failure is swallowed.
    """

    try:
        target = str(Path(cwd).resolve())
    except (OSError, RuntimeError):
        return
    claude_config = Path.home() / ".claude.json"
    try:
        data = (
            json.loads(claude_config.read_text(encoding="utf-8"))
            if claude_config.exists() else {}
        )
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return
    entry = projects.get(target)
    if not isinstance(entry, dict):
        entry = {}
    if entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("allowedTools", [])
    entry.setdefault("hasClaudeMdExternalIncludesApproved", True)
    entry.setdefault("hasClaudeMdExternalIncludesWarningShown", True)
    projects[target] = entry
    try:
        claude_config.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def wait_judge_ready(
    tmux_session: str,
    *,
    backend: str = "claude-code",
    timeout_s: float = 120.0,
) -> tuple[bool, str]:
    """Wait for the configured Judge provider prompt; return (ready, blocker).

    ``blocker`` names an interactive dialog (login_required / trust_prompt /
    usage_limit_reached) detected via the same marker scanner the tmux
    transport uses — a judge must never stall on a silent permission dialog.
    """

    from zf.runtime.backend import get_adapter
    from zf.runtime.provider_interactive_prompt import (
        provider_interactive_prompt_marker,
    )

    backend = _interactive_backend(backend)
    provider_command = "codex" if backend == "codex" else "claude"
    try:
        ready_pattern = get_adapter(backend).ready_pattern
        post_ready_delay_s = get_adapter(backend).post_ready_delay_s
    except ValueError as exc:
        return False, f"unsupported_judge_backend:{exc}"
    deadline = time.monotonic() + max(timeout_s, 1.0)
    grace_deadline = min(deadline, time.monotonic() + 5.0)
    provider_seen = False
    while time.monotonic() < deadline:
        command_leaf = _pane_current_command(tmux_session, JUDGES_WINDOW_NAME)
        command_leaf = command_leaf.rsplit("/", 1)[-1].lower()
        if _pane_provider_alive(tmux_session, backend):
            provider_seen = True
            screen = _capture_pane(tmux_session, JUDGES_WINDOW_NAME)
            blocker = provider_interactive_prompt_marker(screen)
            if blocker:
                return False, blocker
            if re.search(ready_pattern, screen):
                if post_ready_delay_s > 0:
                    time.sleep(post_ready_delay_s)
                return True, ""
        else:
            if provider_seen:
                return False, "provider_process_exited"
            if command_leaf in _SHELL_NAMES:
                if time.monotonic() >= grace_deadline:
                    return False, "provider_launch_not_submitted"
            # else: still booting; keep polling
        time.sleep(_READY_POLL_S)
    return False, "provider_ready_pattern_timeout"


def start_judge_agent(
    tmux_session: str,
    project_root: Path,
    *,
    session_uuid: str,
    system_prompt: str,
    backend: str = "claude-code",
    ready_timeout_s: float = 120.0,
) -> tuple[bool, str]:
    """(Re)start the configured Judge provider; prove readiness.

    Returns ``(ok, error)``; error carries the readiness failure class or
    interactive blocker so the runner can record a proper §11 verdict.
    """

    backend = _interactive_backend(backend)
    if backend not in {"claude-code", "codex"}:
        return False, f"unsupported_judge_backend:{backend}"
    if not ensure_judges_window(tmux_session, project_root):
        return False, "judges window unavailable"
    stop_judge_agent(tmux_session, backend=backend)
    if backend == "claude-code":
        ensure_workspace_trusted(project_root)
    argv = build_judge_argv(
        session_uuid=session_uuid,
        system_prompt=system_prompt,
        backend=backend,
    )
    command = "cd {} && {}".format(
        shlex.quote(str(project_root)),
        " ".join(shlex.quote(arg) for arg in argv),
    )
    last_error = ""
    for attempt in (1, 2):
        # Claude binds the deterministic id on first spawn; Codex creates its
        # own rollout id and therefore has no matching process argument.
        if backend == "claude-code":
            subprocess.run(["pkill", "-f", session_uuid], check=False)
        try:
            _send_line(tmux_session, JUDGES_WINDOW_NAME, command)
        except subprocess.SubprocessError as exc:
            return False, f"failed to launch judge agent: {exc}"
        ready, blocker = wait_judge_ready(
            tmux_session, backend=backend, timeout_s=ready_timeout_s,
        )
        if ready:
            return True, ""
        last_error = f"judge agent not ready: {blocker}"
        if blocker != "provider_launch_not_submitted":
            return False, last_error
        # claude exited immediately (stale session lock or garbled shell
        # input): hard-reset the pane and retry once after a short grace.
        _reset_judges_pane(tmux_session)
        time.sleep(2.0)
    return False, last_error


def send_prompt_to_window(tmux_session: str, prompt: str, *, backend: str = "claude-code") -> None:
    """Paste the rendered packet into the judge agent and submit."""

    _send_line(tmux_session, JUDGES_WINDOW_NAME, prompt, backend=backend)


def _extract_codex_reply(session_path: Path) -> str:
    """Return the last completed assistant turn from a Codex rollout."""

    try:
        lines = Path(session_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    current: list[str] = []
    completed = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if entry.get("type") == "response_item":
            if payload.get("type") != "message" or payload.get("role") != "assistant":
                continue
            content = payload.get("content")
            parts: list[str] = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("output_text") or ""
                        if text:
                            parts.append(str(text))
            elif isinstance(content, str) and content:
                parts.append(content)
            if parts:
                current.extend(parts)
        elif entry.get("type") == "event_msg":
            if payload.get("type") in {"task_complete", "turn_complete"} and current:
                completed = "\n".join(current)
                current = []
    return completed


def extract_assistant_reply(
    session_path: Path,
    backend: str = "claude-code",
) -> str:
    """Return text of the last completed assistant turn, else "".

    A turn is complete only when ``stop_reason == "end_turn"``; streaming
    drafts (``stop_reason`` absent) and tool turns never win.
    """

    if _interactive_backend(backend) == "codex":
        return _extract_codex_reply(session_path)
    try:
        lines = Path(session_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    reply = ""
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(entry.get("type") or "") != "assistant":
            continue
        message = entry.get("message") or {}
        if str(message.get("stop_reason") or "") != "end_turn":
            continue
        texts = [
            str(block.get("text") or "")
            for block in (message.get("content") or [])
            if isinstance(block, dict) and str(block.get("type") or "") == "text"
        ]
        if texts:
            reply = "\n".join(text for text in texts if text)
    return reply


def read_judge_reply(
    project_root: Path,
    session_uuid: str,
    *,
    timeout_s: float,
    poll_s: float = 5.0,
    log: Any | None = None,
    backend: str = "claude-code",
    started_at: float | None = None,
) -> str:
    """Poll the selected provider transcript until the judge turn completes."""

    from zf.runtime.session_tailer import claude_session_path, codex_session_path
    from zf.runtime.backend_session_reader import CodexSessionReader

    backend = _interactive_backend(backend)
    log = log or (lambda message: None)
    path: Path | None = (
        claude_session_path(str(project_root), session_uuid)
        if backend == "claude-code" else None
    )
    codex_reader = CodexSessionReader() if backend == "codex" else None

    def resolve_path() -> Path | None:
        nonlocal path
        if path is not None and path.exists():
            return path
        if backend != "codex":
            return path
        candidate = codex_session_path(session_uuid)
        if candidate is None and codex_reader is not None:
            candidate = codex_reader.session_path(str(project_root), "")
        if candidate is None:
            return None
        if started_at is not None:
            try:
                if candidate.stat().st_mtime < started_at:
                    return None
            except OSError:
                return None
        path = candidate
        return path

    deadline = time.monotonic() + max(timeout_s, 0.0)
    next_heartbeat = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        current_path = resolve_path()
        reply = (
            extract_assistant_reply(current_path, backend=backend)
            if current_path is not None else ""
        )
        if reply.strip():
            return reply
        if time.monotonic() >= next_heartbeat:
            remaining = int(deadline - time.monotonic())
            path_name = current_path.name if current_path is not None else "no transcript"
            log(f"judge still running ({path_name}), {remaining}s left")
            next_heartbeat = time.monotonic() + 60.0
        time.sleep(max(poll_s, 0.5))
    current_path = resolve_path()
    return (
        extract_assistant_reply(current_path, backend=backend)
        if current_path is not None else ""
    )


__all__ = [
    "EVALUATION_WINDOW_NAME",
    "JUDGES_WINDOW_NAME",
    "JUDGE_ROLE_NAME",
    "build_judge_argv",
    "build_judge_role",
    "ensure_judges_window",
    "ensure_workspace_trusted",
    "evaluation_window_enabled",
    "extract_assistant_reply",
    "judge_session_uuid",
    "resolve_judge_backend",
    "read_judge_reply",
    "send_prompt_to_window",
    "spawn_evaluation_window",
    "start_judge_agent",
    "stop_judge_agent",
    "wait_judge_ready",
]
