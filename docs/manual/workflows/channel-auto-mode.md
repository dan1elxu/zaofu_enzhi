# Channel Auto 模式：全自动讨论 → 冻结 canonical PRD

`auto` 是一个 Channel 模板：入口与其他模板一致（Kanban Agent 在
`channel-create-and-start` Plan 中指定 `auto` 模板，Owner 批准 Plan 后
讨论自动开始）。批准 Plan 即完成唯一的 Owner 授权点；之后整条链无人介入，
讨论终点先写入 `channel.consensus.reached`，随后由统一的
`channel.prd.frozen` occurrence 交接不可变 PRD 评测材料。前者是内部共识事件，
后者是评测 observer 的唯一触发入口。
Task/Workflow 规划属于冻结后的独立 Owner 动作，不由 auto Channel 自动创建。

## 全自动链

```text
kanban agent: channel-create-and-start(template=auto, message=问题)
  -> multi_lens 讨论（fanout -> relay），discussion.moderator=true 时
     由主持人（channel-moderator，KanbanHeadlessAgent 边缘 turn）主持：
       complexity.decided（按任务复杂度定预算：simple/standard/complex）
       -> 每轮 quiet 后 verdict.requested：决定继续（round.opened，带
          @提及的发言名单与焦点）或收敛（synthesis -> phase3）
       -> LLM 失败走确定性 fallback（按预算 continue/conclude），永不卡死
       -> 预算耗尽硬收敛（budget_exhausted）；router 的 round guard
          以该预算为显式上限（简单任务不会无限讨论）
  -> cross-review -> synthesis
  -> 开放问题自动答复（channel-auto-chain resolver LLM turn）
       resolved_by=owner:auto-resolver（非 agent 成员，fold 既有规则直接接受；
       evidence 类由目标成员举证）
  -> 全 roster 签名 consensus（auto 不要求 human_confirmed；
       Owner 确认已在 create-and-start Plan 批准时前置完成）
  -> PRD 冻结（所有必需成员签署、无开放问题/待修订、文件校验通过）
  -> channel.consensus.reached（内部 canonical consensus）
  -> channel.prd.frozen（冻结评测材料，触发统一 Judge）
```

主持人是边缘 LLM 角色（actor=`channel-moderator`），不是内核状态机：内核
（`channel_moderator.py` fold + `advance_moderator_round`）持有预算、门禁与
fallback，LLM 只做两件事——讨论开始时的复杂度分类、每轮安静后的
continue/conclude 裁决。讨论何时冻结由 conclude 裁决 + 收敛门禁（无开放问题、
cross-review 完成）共同决定；预算永远是硬上限。

## 内核行为差异（全部按 session 的 discussion.auto 标志门控）

| 位置 | 行为 |
|---|---|
| `channel_templates.py` | `auto` 模板：成员同 `prd-clarification`，`discussion.mode=multi_lens` + `auto: true` + `moderator: true`；`discussion_mode: "auto"` override 显式有界 |
| `channel_contracts.py` | `channel_discussion_auto_enabled()` 规范化读取 auto 标志；`channel_discussion_moderator_enabled()` / `channel_discussion_budget_max_rounds()` 读 moderator 标志与复杂度预算 |
| `channel_projection.py` | `_apply_discussion_mode` 把 payload `auto`/`moderator` 存入 discussion config（digest 绑定）；fold `complexity_decisions` / `moderator_rounds` / `moderator_verdicts` |
| `channel_discussion.py` | `_consensus_reached` auto 分支：全 roster 签名即可，不要求 `human_confirmed`；moderator 启用时 phase2 推进委托 `advance_moderator_round`，收敛经 `request_synthesis_and_phase3`（带 conclude 门禁） |
| `channel_moderator.py` | 新 sibling：复杂度分类、round 预算、verdict 裁决 + 确定性 fallback、`register_moderator_handlers()` |
| `channel_router.py` | `_debate_round_guard_reason` 无显式 max_rounds 时读复杂度预算为硬上限 |
| `channel_auto_chain.py` | resolver + `register_auto_chain_handlers()`；冻结后不自动启动 handoff |
| `orchestrator.py` | `channel.discussion.started` 与 5 个 moderator 事件进 `_KERNEL_LIVENESS_EVENTS`；Layer-1 fallback 对无 task 事件跑 builtin side-effect handlers（auto-e2e 2026-09-16 修复） |
| `orchestrator_reactor.py` | 注册 auto-chain + moderator 反应器（仅 call site）；auto-chain delegate 异常落日志 |
| `wake_patterns.py` | `channel.consensus.reached` 与 moderator 事件入 wake 集合；评测 observer 只消费 `channel.prd.frozen` |
| `known_types.py` / `event_problem_registry.py` | `channel.auto_chain.step|completed|failed` 注册；failed 走 human recovery；`channel.moderator.verdict.fallback` 注册为 informational（重复 fallback 才需人工） |

关键发现：`channel-question-resolve` action 本身无 Owner-authority 检查，fold
只拒绝 actor/resolved_by 为 agent 成员的 `answered` resolution。因此 resolver
以 `owner:auto-resolver`（非成员）身份 resolve，fold 零改动；语义判断全在边缘
LLM turn，内核保持确定性。

## 不变量

- 幂等：冻结后的讨论不再调度答题或 handoff；旧 handoff 失败也不会恢复。
  verdict.requested 按 round 幂等，digest 变化才重新裁决。
- readiness 纪律：当前 canonical PRD 可进入 Task/Workflow 规划。历史
  `needs_multi_lens`/`needs_owner`、gaps 和 synthesis 问题列表不拦截规划；
  原始 readiness 保留给实际执行检查，签署不改写已签文件。
- 终点即止：链只到 canonical PRD；Task 与 Workflow 规划仍是独立 Owner 动作。
- 语义在边缘：resolver/主持人的判断全在 KanbanHeadlessAgent turn（backend 选择
  同 workflow_synthesis：`ZF_KANBAN_AGENT_BACKEND` → `config.orchestrator.backend`）。
- 讨论有界：复杂度预算（simple=max(4, 2×成员)、standard=默认、
  complex=min(256, 8×成员)）是硬上限；LLM 分类/裁决失败降级确定性 fallback，
  永不因边缘故障卡死讨论。冻结需过收敛门禁（无开放问题 + cross-review 完成）。

## 验证

- `tests/test_channel_auto_mode.py`：模板物化、fold、consensus auto/非 auto 对照、
  resolver 答复 owner 问题、不同 readiness 下的冻结、非 auto 确认门。
- `tests/test_channel_auto_recovery.py`：冻结后不创建 Task/Workflow、不恢复旧 handoff。
- `tests/test_channel_templates.py`：`discussion_mode: "auto"` override 有界合法。
- `tests/test_channel_moderator.py`：复杂度预算、quiet→verdict 请求、continue 开轮
  （@提及 + 焦点）、conclude 收敛进 phase3、开放问题挂起、预算耗尽硬收敛、
  router 预算门禁、LLM 失败 fallback；含经 `Orchestrator.run_once` 的真实 dispatch
  回归（L2-active 配置下 task-less 事件不再被 notify-only 分支吞掉）。

## 后续（不在本步）

- 真实 provider 端到端跑通整条链。
- Workflow Plan 批准/启动的自动化（当前仍为 Owner 手动）。
