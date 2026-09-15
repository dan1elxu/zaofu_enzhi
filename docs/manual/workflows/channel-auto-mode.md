# Channel Auto 模式：全自动讨论 → 冻结 PRD → Task + Workflow Plan

`auto` 是一个 Channel 模板：入口与其他模板一致（Kanban Agent 在
`channel-create-and-start` Plan 中指定 `auto` 模板，Owner 批准 Plan 后
讨论自动开始）。批准 Plan 即完成唯一的 Owner 授权点；之后整条链无人介入，
终点是 Task 创建 + Workflow Plan 发布（`workflow.request.proposed`），
**不** emit `workflow.invoke.requested`、不启动 Workflow。

## 全自动链

```text
kanban agent: channel-create-and-start(template=auto, message=问题)
  -> multi_lens 有界讨论（fanout -> relay -> cross-review -> synthesis）
  -> 开放问题自动答复（channel-auto-chain resolver LLM turn）
       resolved_by=owner:auto-resolver（非 agent 成员，fold 既有规则直接接受；
       evidence 类由目标成员举证）
  -> 全 roster 签名 consensus（auto 不要求 human_confirmed；
       Owner 确认已在 create-and-start Plan 批准时前置完成）
  -> PRD 冻结（readiness 仍需 ready；
       needs_owner 时链失败并落 channel.auto_chain.failed，人工接管）
  -> handoff LLM turn（KanbanHeadlessAgent，按 PRD 产出 task payload + workflow_plan）
  -> create-task controlled action（payload 带 channel_authority）
  -> task.created + workflow.request.proposed + channel.auto_chain.completed
```

## 内核行为差异（全部按 session 的 discussion.auto 标志门控）

| 位置 | 行为 |
|---|---|
| `channel_templates.py` | `auto` 模板：成员同 `prd-clarification`，`discussion.mode=multi_lens` + `auto: true`；`discussion_mode: "auto"` override 显式有界 |
| `channel_contracts.py` | `channel_discussion_auto_enabled()` 规范化读取 auto 标志 |
| `channel_projection.py` | `_apply_discussion_mode` 把 payload `auto` 存入 discussion config（digest 绑定） |
| `channel_discussion.py` | `_consensus_reached` auto 分支：全 roster 签名即可，不要求 `human_confirmed` |
| `channel_auto_chain.py` | 新 sibling：resolver / handoff runner + `register_auto_chain_handlers()` |
| `orchestrator_reactor.py` | 注册 4 个事件的 auto-chain 反应器（仅 call site） |
| `wake_patterns.py` | `channel.consensus.reached` 入 wake 集合 |
| `known_types.py` / `event_problem_registry.py` | `channel.auto_chain.step|completed|failed` 注册；failed 走 human recovery |

关键发现：`channel-question-resolve` action 本身无 Owner-authority 检查，fold
只拒绝 actor/resolved_by 为 agent 成员的 `answered` resolution。因此 resolver
以 `owner:auto-resolver`（非成员）身份 resolve，fold 零改动；语义判断全在边缘
LLM turn，内核保持确定性。

## 不变量

- 幂等：同 `source_digest` 已有 `task.created` 或 `channel.auto_chain.completed`
  时 handoff 直接跳过；`_IN_FLIGHT` 线程级防重。
- readiness 纪律：PRD canonical 但 planning 未授权（needs_owner）时链失败
  （`channel.auto_chain.failed`，recovery_policy=human），PRD 保持冻结可人工接管。
- 终点即止：链只到 Workflow Plan 发布；批准与启动仍是独立 Owner 动作。
- 语义在边缘：resolver/handoff 的判断全在 KanbanHeadlessAgent turn（backend 选择
  同 workflow_synthesis：`ZF_KANBAN_AGENT_BACKEND` → `config.orchestrator.backend`）。

## 验证

- `tests/test_channel_auto_mode.py`：模板物化、fold、consensus auto/非 auto 对照、
  resolver 答复 owner 问题、handoff 建 Task + 发布 Plan、幂等、readiness 拒绝、非 auto 跳过。
- `tests/test_channel_templates.py`：`discussion_mode: "auto"` override 有界合法。

## 后续（不在本步）

- 真实 provider 端到端跑通整条链。
- Workflow Plan 批准/启动的自动化（当前仍为 Owner 手动）。
