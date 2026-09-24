# Channel auto 调用失败与恢复

`channel_auto_chain.py` 的自动答题与 PRD 交接由
`channel_auto_recovery.py` 包装。保留既有讨论、PRD 和 Task 创建 gate，
不新增独立队列或常驻服务；已有讨论 tick 负责找回未结束的步骤。
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
auto 与人工模式共用。`test_channel_discussion_driver.py` 中的
`test_synthesis_questions_trigger_next_generation` 覆盖新稿落盘、版本关联、
重复与旧回复，以及两种模式的共识签收边界。
