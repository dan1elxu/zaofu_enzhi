# Channel 到 PRD

[English](channel-to-prd.en.md) · [工作流索引](README.md)

> 当前合同：Channel 是面向人和 Agent 的需求讨论室。默认是自然 conversation；
> clarification 和 multi-lens 需要显式选择。创建 Channel 不等于自动 fanout，确认 PRD
> 也不等于创建 Task 或启动 Workflow。

## 什么时候使用

适合：

- 原始需求存在多个解释，需要 Owner、产品、架构、安全或质疑视角共同澄清；
- 结论需要跨 Web、飞书和 Kanban Agent 精确回到原始发起入口；
- 希望先形成 canonical PRD，再决定是否进入软件交付。

不适合：

- 已经清晰且只需一个 Agent 在当前 session 内处理的小问题；
- 已有 Task，只需要选择 Workflow；
- 用群聊成员直接修改 Project 或绕过审批点火。

## 三种产品模式

| 模式 | 何时使用 | 行为 |
|---|---|---|
| `conversation` | 默认；自然讨论和定向 @mention | 用户或 Leader 决定谁继续回答，不自动 fanout |
| `clarification` | 需求缺字段、边界或 owner 决策 | 围绕开放问题逐项澄清 |
| `multi_lens` | 明确需要多个独立视角和综合 | 显式 Discuss 后执行有界 fanout/synthesis |
| `auto` | 用户明确要求讨论阶段无人介入 | 批准 create-and-start Plan 后自动讨论并冻结 PRD；冻结结束，Task/Workflow Plan 独立规划 |

底层兼容名如 `manual_mention`、`mention_relay`、`fanout_then_synthesis` 是 engine
映射，不应作为并列的新产品模式展示。

## Auto 模式：自动讨论链（到冻结为止）

`auto` 模板复用 multi_lens 讨论机制，但把讨论阶段的人工关卡改为前置授权或自动化。
这条链只负责讨论、全文评审和 canonical PRD 冻结；冻结后不自动创建 Task，也不自动
生成或提交 Workflow Plan。

1. **Owner 确认前置**：批准 `channel-create-and-start` Plan 即完成唯一授权点，
   该授权覆盖本次 auto 讨论与冻结，不覆盖冻结后的 Task/Workflow 规划或审批；之后
   consensus 不再要求逐次 human_confirmed（以全 roster 签名为门）。
2. **开放问题自动答复**：phase2 relay 中出现开放问题时，channel-auto-chain
   的 resolver 边缘 LLM turn 自动逐条 resolve（`resolved_by=owner:auto-resolver`），
   并在 Channel 中回帖说明。
3. **PRD 冻结**：全 roster 签名、问题和全文评审门通过后，先写入
   `channel.consensus.reached`，再写入带不可变快照的 `channel.prd.frozen`；后者是
   评测的唯一入口。

链终点是 `channel.prd.frozen`，不批准、不启动 Workflow，也不负责冻结后的
Task/Workflow Plan。当前 PRD 冻结后可直接发起 Plan Task；旧 readiness 与 synthesis
问题列表不再拦截规划，readiness 保留给实际执行检查。实现细节见 [Channel Auto 模式](channel-auto-mode.md)。

## 创建 Channel

可以从 Kanban Agent 请求：

```text
为登录安全改造创建一个 PRD 讨论 Channel。
先按自然 conversation 建群，邀请产品、架构和安全视角；不要自动创建 Task 或启动 Workflow。
```

审核 Channel setup Plan 中的：

- template、Channel name 和 Owner；
- required/optional members；
- provider/model override；
- discussion mode 和预算；
- Leader、权限和允许的 handoff；
- source/origin receipt 目标。

选择创建后，系统物化 Channel、Members、skills、权限和原始需求。默认
`conversation` 不会因为 Channel 创建完成就自动 fanout 所有成员。

## 讨论和收敛

典型路径：

```text
origin request
  -> durable message ACK
  -> natural conversation
  -> optional clarification
  -> optional explicit Discuss / multi_lens
  -> explicit Finalize
  -> PRD draft
  -> Continue | Revise | Owner confirm
  -> channel.consensus.reached (internal consensus)
  -> channel.prd.frozen(ref, digest, revision)
  -> exact-origin PRD receipt
```

![Channel 中从自然讨论到多角色收敛的动态过程](../assets/quickstart-channel-discussion.webp)

讨论中可以：

- @mention 一个成员回答具体问题；
- 补充约束并继续同一 thread；
- 显式触发多视角讨论；
- 查看成员 identity、provider、状态和权限；
- 要求 Finalize 形成 PRD draft；
- 继续、修订或由 Owner 确认某个 revision。

普通模式只有 Owner 确认的 revision 才能进入 canonical PRD 冻结；auto 模式以批准
`channel-create-and-start` Plan 作为本次讨论与冻结的前置 Owner 授权，之后仍须通过
全 roster 签名和其他机械门禁。`channel.consensus.reached` 是冻结前的内部共识事件；
随后写入的 `channel.prd.frozen` 才是带有不可变快照的 canonical PRD handoff，也是评测的
唯一入口。普通 synthesis、某个 Agent 的总结或多数成员口头同意都不能替代 Owner authority。

## 从 PRD 进入交付

普通或 auto Channel 冻结 PRD 后仍需独立规划和交接：

```text
frozen canonical PRD
  -> existing Task
     or Create Task from PRD proposal -> human confirm
  -> Task-bound Workflow Plan
  -> exact Workflow proposal
  -> Approve
  -> Kernel starts Workflow
  -> read-only Task/Run/Delivery receipt back to the original Channel
```

只有 Channel 的 exact `leader_member_id` 且拥有 `propose_workflow` 权限，才能发起
Workflow handoff proposal。它仍不能批准自己的 proposal，也不能持有
`ZF_WORKFLOW_ACTION_TOKEN`。

## CLI 补充输入

稳定的低层消息命令是：

```bash
zf channel say CHANNEL-ID \
  --text "请补充失败场景，并由 @critic 复核。" \
  --member-id reviewer \
  --mention critic
```

Finalize、Owner confirm、成员权限和 Workflow handoff 当前主要通过 Web/Kanban Agent、
Feishu 或受控 action 入口完成，不应通过手写 `events.jsonl` 模拟。

## 观察与故障

重点检查：

- 原始消息是否仅入账一次，ACK/NACK 是否明确；
- 当前 product mode 是否为预期值；
- multi-lens 是否由显式动作启动；
- PRD ref、digest、revision 和 owner identity 是否齐全；
- receipt 是否回到 exact origin；
- Task/Workflow proposal 是否与 confirmed PRD revision 绑定；
- 非 Leader 或无权限成员的 handoff 是否被拒绝；
- provider 失败是否被去重并留下可恢复状态。

```bash
zf events --last 100
zf status --workers
```

## 完成定义

Channel-to-PRD 完成只表示：PRD 已按 Owner 逐次确认或 auto 前置授权冻结，有
ref/digest/revision 和 `channel.prd.frozen` 可追溯，且来源收到回执。之后独立创建真实
Task、批准 Workflow 并由 Kernel 执行，才进入交付闭环。

## 相关

- [15 Channel 深度使用手册](../15-channel-collaboration.md)
- [Channel Auto 模式](channel-auto-mode.md)
- [受控 Workflow 启动](controlled-workflow-start.md)
- [Feishu AI-Native Bridge](../19-feishu-ai-native-direct-bridge.md)
