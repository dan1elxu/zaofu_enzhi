# 真实 Codex Provider Preflight

> 适用对象: 运行真实 Codex E2E、Channel Codex provider、真实 provider smoke 的操作者。

## 1. 先跑预检

```bash
uv run zf doctor provider --backend codex
uv run zf doctor provider --backend codex --json
```

预检只读取环境,不会启动 ZaoFu worker,也不会写 runtime truth。它检查:

- `codex` CLI 是否在 `PATH`。
- `codex --version` 是否可执行。
- 当前环境是否支持基础 network namespace probe。

如果输出 `sandbox: unsupported`,普通 Codex sandbox 可能在启动前失败,常见错误类似:

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
Codex sandbox unsupported for sandbox=workspace-write:
unshare: unshare failed: Operation not permitted
```

## 2. E2E 策略

真实 E2E 不允许自动降级 fake provider。预检失败时只有两种选择:

1. 修宿主机 namespace / sandbox 权限,再重跑预检。
2. 在报告中显式记录风险,并仅对本次真实验证使用 Codex sandbox bypass。

示例:

```bash
codex exec --dangerously-bypass-approvals-and-sandbox --json "$PROMPT"
```

ZaoFu 当前把该 bypass 作为可信本地 Codex provider 的默认执行权限；它适用于
受控开发机、E2E 和已授权项目，不适用于共享主机、非可信项目或 production-like 环境。

### 2.1 WebKanban 启动模式

Channel 的 `permission_profile` 是 ZaoFu 的语义写入约束，不再选择 provider OS
sandbox。所有 Codex headless turn 默认使用 `danger-full-access`，所有 Claude
headless turn 默认使用 `bypassPermissions`；Kernel 的 write-policy、artifact
路径和 controlled action gate 继续独立执行。

可信本地开发机应使用 canonical launcher：

```bash
tools/start-webkanban.sh --host 127.0.0.1 --port 8001
```

该 launcher 会加载 action token、Workspace provider 环境，并在未显式覆盖时为
可信本地 WebKanban 设置：

```text
ZF_KANBAN_AGENT_CODEX_HEADLESS_SANDBOX=danger-full-access
ZF_KANBAN_AGENT_CODEX_HEADLESS_APPROVAL_POLICY=never
ZF_KANBAN_AGENT_CLAUDE_HEADLESS_PERMISSION_MODE=bypassPermissions
```

检查实际启动策略：

```bash
tools/start-webkanban.sh --port 8001 --status
```

必须看到 `codex_headless_sandbox: danger-full-access`、
`claude_headless_permission_mode: bypassPermissions`、`tmux: running` 和
`api: ok`。`zf doctor provider` 仍可能报告宿主普通 sandbox 不可用；它检查的是
host capability，不是 ZaoFu 是否已显式 bypass。

直接 `uv run zf web ...` 也会使用相同的代码默认值；以下变量仅用于显式记录或
覆盖 launcher 进程：

```bash
export ZF_KANBAN_AGENT_CODEX_HEADLESS_SANDBOX=danger-full-access
export ZF_KANBAN_AGENT_CODEX_HEADLESS_APPROVAL_POLICY=never
export ZF_KANBAN_AGENT_CLAUDE_HEADLESS_PERMISSION_MODE=bypassPermissions
uv run zf web --host 127.0.0.1 --port 8001
```

`danger-full-access` 与 `bypassPermissions` 会关闭 provider 原生的常规权限门。
它们只适用于明确受信任的本地代码库和网络；共享主机、非可信项目或
production-like 环境不应使用此默认部署模型。

### 2.2 CPA 中转站与 tmux worker

如果 Codex 使用 CPA 中转站，先在启动 `zf` 的同一个 shell 加载 CPA 环境：

```bash
source ~/.config/cpa/defaults.env
```

Codex 的中转配置必须位于 `~/.codex/config.toml`，至少包含
`model_provider`、`model_providers.<name>.base_url` 和对应的 `env_key`。
ZaoFu 为每个 role 使用独立的 `CODEX_HOME`；每次 worker 启动都会刷新这份配置，
并通过 tmux 的受控环境更新传递 `CPA_API_KEY`，避免旧 role 配置把 Codex 带回
ChatGPT 登录页。切换中转站后请重启整组 worker：

```bash
uv run zf stop
uv run zf start
tmux attach -t zf-test_dev5
```

不要把真实 CPA key 写进 pane 命令或日志；检查时只显示变量名和脱敏值。

## 3. Channel 失败判断

Channel / Kanban Agent 使用真实 Codex provider 时,如果 sandbox 或 app-server 启动失败,期望事件是:

- `channel.agent.reply.started`
- `channel.agent.reply.failed`

Web 应显示失败原因,而不是把 provider 环境问题伪装成 agent 已复核或 task done。排查时查看:

```bash
uv run zf events --last 80
uv run zf doctor provider --backend codex --json
```

Codex app-server 可能在 stderr 输出:

```text
Codex could not find bubblewrap on PATH ... Codex will use the bundled bubblewrap ...
```

这条本身是非致命 warning,不应被当作失败根因。若 channel 显示
`timeout`,优先判断是否是 Codex app-server 在 channel provider budget 内
没有继续输出事件。Codex turn 没有总时长上限:持续有 token / tool /
status 流式事件就会续期。默认普通静默预算为 1800 秒;检测到工具调用
尚未完成时,静默预算切换为 7200 秒。可按本地场景显式覆盖:

```bash
export ZF_CHANNEL_PROVIDER_HEADLESS_TIMEOUT_S=3600
export ZF_CODEX_HEADLESS_TOOL_TIMEOUT_S=14400
```

旧的 `ZF_KANBAN_AGENT_HEADLESS_TIMEOUT_S` 仍会被兼容读取,但 channel 场景
优先使用 `ZF_CHANNEL_PROVIDER_HEADLESS_TIMEOUT_S`。

## 4. 脱敏要求

不要打印真实 token。检查环境变量时只输出 key 或脱敏值:

```bash
env | grep -E 'CODEX|OPENAI|ZF_' | sed -E 's/(=.).+$/=***REDACTED***/' | sort
```
