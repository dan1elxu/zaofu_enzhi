# Channel auto 调用失败与恢复

`channel_auto_chain.py` 的自动答题由
`channel_auto_recovery.py` 包装。保留既有讨论、PRD 和 Task 创建 gate，
不新增独立队列或常驻服务；已有讨论 tick 负责找回未结束的答题步骤。
auto 链终点是 canonical PRD 冻结，冻结后不自动调度 Task/Workflow 交接，
也不恢复或重试旧 handoff 步骤；历史事件仍保留。
评测 Judge 的调度与恢复见 [PRD Evaluation 实施说明](prd-evaluation-implementation.md)。

- 每轮最多尝试 3 次，失败后分别等待 1 秒、2 秒重试；异常、provider 失败、
  不完整响应均保留原因，不能用部分回复冒充成功。原有 JSON/合约修复仍有独立上限。
- 每次调用前通过 `EventWriter` 写 `channel.auto_chain.step`，记录
  `operation_id/channel_id/thread_id/purpose/source_id/attempt/max_attempts/status/reason`。
  失败中间态为 `retrying`，耗尽后写 `failed` 和 `channel.auto_chain.failed`。
  终态不会被周期扫描重新尝试；部分答题也不会重置次数。
- watcher 重启后，步骤锁和 headless 调用锁均已释放的 `running` 调用标记为
  `interrupted`，沿用剩余次数。中断原因未知时明确标注未知，不推断网络或鉴权故障。
  旧版缺少 step 事件的隐藏调用，也可从现有讨论状态和 headless thread 找回。
- 短只读 auto turn 的无进展超时默认 300 秒，可通过
  `ZF_CHANNEL_AUTO_IDLE_TIMEOUT_S` 调整；Codex 无输出的工具等待使用同一预算。
  普通 Kanban 调用沿用原策略。auto 关闭内层 transport 重试以免叠加次数。
- Channel 页面展示当前步骤、尝试次数和原因。失败时可点“重试”：已有 token-gated
  Web action `channel-auto-retry` 校验失败 `operation_id`，只写
  `channel.auto_chain.retry.requested`；watcher 收到后开启新一轮，`retry_of` 保留追溯。
  重复点击、过期操作或正在执行的步骤会被拒绝。

实现接线：`tick_services._sweep_channel_discussions`、auto-chain reactor 注册、
`wake_patterns`、`ControlledActionService` 和 Channel conversation projection。
回归入口：`tests/test_channel_auto_recovery.py` 与 `tests/test_channel_auto_mode.py`；
provider 传输回归使用 `tests/test_web_headless_agent.py` 的模拟进程。

Channel reactor 在 ingress 锁内仅持久化路由与回复请求，释放锁后通过现有后台
dispatcher 调用 provider。综合、修复、问题合并、交叉评审、签署评审和自适应
下一轮共用此路径；慢调用不能占住 watcher 或挡住其他频道，后台完成事件仍由
正常 reactor 推进讨论。派发期间持有每个回复的跨进程 claim；超过 900 秒只表示
需要检查，恢复扫描必须先确认 claim 已释放，并重读当前事件，才能递增 generation。
进程退出后 claim 自动释放，下一次扫描可继续恢复，活跃长调用不会被误作过期回复。
`tests/test_channel_runtime_liveness.py` 通过真实 `Orchestrator.run_once` 验证慢综合
不阻塞其他频道和 tick，并覆盖活跃与已遗失回复的恢复边界。

主持人的轮次边界等待同线程所有 `pending/queued/running/started` 回复结束；
请求判断与消费已返回判断共用这个条件，防止排队回复尚未执行就连续开轮，
或判断期间新增回复尚未完成就提前进入综合。回归见 `tests/test_channel_moderator.py`。
轮次事件绑定发言通知的 `message_id`；通知已写入但回复尚未路由、或只登记了部分
指定成员时同样等待。明确的路由拒绝/抑制允许结束这段等待。判定按当前线程消息和
完整问题账本 digest 绑定；账本变化时在同一轮重新请求判断，不把过期判断机械改成
继续开轮。请求幂等及 provider turn 身份均包含 digest，晚到旧判断不能覆盖新判断。
复杂度与轮次判断读取原始需求正文，轮次判断同时读取当前问题的完整答案及解决类型；
`conclude` 仅进入综合，最终冻结仍走全文评审。显式假设及后续实施验收要求必须保留，
不能把未来测试结果尚不存在误判为重复讨论的前置条件；过期判断按账本 digest 拦截。
综合修复耗尽后的 `channel.synthesis.blocked` 同样唤醒已注册的有界恢复 handler，
避免修复事件只能等待其他无关事件驱动。

浏览器验收使用 `web/tests/channel-auto-retry.spec.ts`：仅连接隔离 fixture API，
fixture 需使用隔离的 `ZF_WORKSPACE_HOME` 并完成或跳过首次引导，
有有效 `zf.yaml`（自定义 `project.state_dir`）、TaskStore 和 EventWriter
初始化的 auto Channel `ch-auto`，其答题步骤为第 3 次失败，原因
`Selected model is at capacity`。设置 `ZF_AUTO_RETRY_E2E=1`、
`ZF_WEB_BASE_URL` 指向测试 API（8002+），测试 token 为 `auto-retry-test`；
使用 `mcp/playwright:latest` Docker 镜像运行，避免连接真实运行态。

## 补充答案后的综合轮次

PRD 草案提出新问题、回退讨论并完成答题后，新一轮综合以其正式的
`channel.synthesis.requested` 事件为判旧边界。接收回复时即使已回到
`phase3_synthesis`，也允许通过合同校验的结果修订该请求之前的草案；
请求之后已有其他综合结果时，仍保留晚到回复的过期保护和重复消费幂等性。
该规则由 `channel_reply_contract._ignore_superseded_synthesis_reply` 实现，
auto 与人工模式共用。已落盘 `channel.synthesis.proposed` 的请求不再视为
in-flight；补充讨论重新进入 phase2 后，必须生成新的
`channel.synthesis.requested`，同时同一请求的重复派发仍保持幂等。
`test_channel_discussion_driver.py` 中的
`test_synthesis_questions_trigger_next_generation` 覆盖新稿落盘、版本关联、
重复与旧回复，以及两种模式的共识签收边界。
晚到的共识 blocker 如果已经在答题阶段被处理，投影会保留当前
`phase2_relay` 标记；重新进入综合后不会因为旧 blocker 再次回退答题阶段。

## PRD 全文评审与冻结

auto Channel 沿既有讨论状态机执行：独立发言 → 问题合并与答题 → 综合 PRD →
全文评审。评审提出阻塞问题时回到答题，再生成下一版 PRD；只有当前版本的
必需成员全部签署、没有开放问题或待修订标记、文件校验通过时，才写入
`channel.consensus.reached`；该事件只是冻结前的内部共识事件。随后写入的
`channel.prd.frozen` 才形成带不可变快照的 canonical PRD handoff，并作为评测的唯一入口。
当前冻结共识允许后续独立 Task/Workflow 规划；readiness 保留给实际执行检查，不决定是否评测。auto 模式的 Owner
授权来自创建时批准的 `channel-create-and-start` Plan，因此冻结前不再等待逐次人工确认；
人工模式保留确认门。

Web 已冻结版本显示“PRD 已冻结 · canonical PRD”、版本与文件引用，并允许
Plan Task/Plan Workflow，即使旧 readiness 仍为 `needs_multi_lens` 或 synthesis
保留历史 open_questions。规划与 Task 创建校验当前共识、Leader、版本和文件摘要；
历史 readiness 与 gaps 留在原文件并随 Task 传递，实际执行仍检查 readiness。

`channel_semantic_sources.py` 识别 `consensus_review_id`，在 provider 派发前按
proposal 的 artifact ref 和 SHA256 读取完整 PRD，放入
`semantic_source_documents`。评审不依赖模型自行读取文件，也不依赖历史消息摘要。
缺失、越界、哈希不符或全文超过预算都明确拒绝，不能截断后继续签署。

修订综合使用同一轮讨论内上一版完整 PRD checkpoint、原始需求、上一轮综合
触发之后的新消息及完整问题账本。综合运行期间到达的意见也必须保留；已吸收的
旧讨论和重复综合回复不再反复拼接。checkpoint 同样校验 ref 和哈希。

评审结果绑定 proposal event id、PRD revision 和 artifact digest。新版本或
重评后到达的旧签名、旧 blocker 不得污染当前轮。冻结前再次读取 PRD、readiness、
conclusion 三份 sidecar，核验 SHA256、revision 和交叉引用；失败保留原因，
重复 tick 不重复刷同一条拒绝事件。

## 修复上下文后定向重评

历史频道因评审未收到正文而停滞、但当前 PRD 本身完整时，owner 或具有精确授权的
delegate 可使用受控操作，重新评审同一版 PRD：

```sh
zf channel review-retry CHANNEL \
  --consensus-event-id CURRENT_PROPOSAL_EVENT \
  --reason '已修复评审全文上下文，重新评审当前版本' --actor OWNER
```

该入口经 `ControlledActionService` 执行；Web 同名 action
`channel-consensus-review-retry` 受 token 门禁保护。操作校验当前 proposal、owner
权限、sidecar 完整性，并拒绝进行中的回复、开放问题、待修订或已冻结状态。
通过后保留原 PRD ref/digest/revision，追加新 proposal 并由正常 reactor 派发真实
评审。它不修改旧账本、不生成签名；重复提交旧 proposal id 会被拒绝。

确定性回归入口为 `tests/test_channel_consensus_context.py` 和
`tests/test_channel_prd_freeze.py`：通过实际 action/router/reactor 路径验证
r1 blocker → 答题 → r2 → 评审签署 → 唯一冻结事件，并覆盖损坏证据、旧轮回复、
重评幂等及 Web token gate。真实 provider 验收另行从 Kanban Agent 产生
`channel_setup` Plan，经 `kanban-plan-apply` 创建并启动 auto Channel，再核查真实
讨论、PRD/readiness sidecar、`channel.consensus.reached` 与其后精确对应的
`channel.prd.frozen`（含 source snapshot 和 digest）。仅有 reached 不能证明冻结完成；
还须核查 eval 从该 frozen occurrence 入队，并未自动创建 Task 或发布 Workflow Plan。
模拟回归不能替代这项验收。
