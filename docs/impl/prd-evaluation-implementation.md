# PRD Evaluation 实施说明

本实现把 Kanban/Channel 的冻结快照送入一个持久化、有界并发的 Judge 队列。评估事件是只读投影事件，不改变任务、Channel 或 PRD 的权威状态。

## 流程

1. `evaluation_watch` 读取 `events.jsonl`，只以统一的 `channel.prd.frozen` 为评测入口；`channel.consensus.reached` 是冻结前的内部讨论事件，不单独触发评测。observer 在冻结引用的 source occurrence 处投影，校验 matching synthesis 且无开放问题/待修订。`multi_lens` 要求必需成员完整签署，`conversation` / `clarification` 要求 owner 已确认。结果 receipt 不会单独启动 Judge，readiness 不决定是否评测。
2. 冻结 handoff 同时携带带 `channel/thread/revision` 身份的 source snapshot 及 digest；observer 以其引用的 consensus/source occurrence 为材料截止点，并核对快照中的证据清单。consensus 与 freeze 之间的新增消息不进入 packet。按 ref 读取冻结 PRD/readiness，校验 PRD 原始字节与冻结 SHA-256；消息正文 sidecar 也必须完整读取并通过摘要校验，缺失或损坏不能回退到 preview。材料缺失、越界、摘要非法或不一致时不调用 provider。版本化 PRD 还需匹配 channel/thread/revision。通过后生成不可变 packet，写入 `artifacts/benchmark-evaluations/queue-inputs/`，其中包含 `workflow_run_id`、`trial_id`、`judge_type` 和 `source_snapshot_digest`。已合法入队的快照继续用于恢复，不依赖源文件仍存在。
3. 统一冻结入口进入 `evaluation_scheduler.py` 的五个阶段：`task_type → contribution、discussion、prd_fidelity → conclusion`。`task_type` 只能从 `bugfix`、`refactor`、`greenfield`、`ambiguous` 中选择；只有明确类型才会绑定对应的三份冻结 role.md。三个语义 subagent 只读同一个 frozen packet、自己的 role 区块和自己的 scope，互不读取其他 subagent 结果；主 Judge 只汇总原始结果。同一 channel 严格按依赖执行；不同 channel 可以占用不同 slot。默认 2 个 slot，可用 `ZF_EVALUATION_JUDGE_SLOTS` 调整（上限 8）。
4. 每个 slot 使用独立的 `judges-slot-N` tmux window、provider launch UUID、Codex `CODEX_HOME`/session root 和 binding sidecar。observer 只发现并入队，不等待 provider。
   Judge 在独立 input 目录读取本次 packet 副本；Claude 仅开放 Read，禁止权限询问并禁用 MCP/Hook；Codex 固定 `read-only` + `never`，禁用 Hook、Apps、Plugins 和用户配置的 MCP，不接受 worker sandbox 放宽。只读沙箱限制写入，不等于操作系统级的 packet-only 文件读取隔离。
5. Judge turn 通过 `evaluation_turns.py` 按当前 provider session、rollout 和 turn 过滤 transcript。Codex 必须匹配本次 prompt marker；不会按项目目录“最新 rollout”猜测。Claude 只把当前 user marker 后的 assistant 内容作为结果。
   Codex 的带 marker 提示通过 CLI 初始 prompt 参数提交，避免 TUI 启动或粘贴吞掉字符/提交键；时间边界在启动前记录。Judge 调用关闭启动更新检查，更新菜单不能被当作就绪输入框；CLI 升级由 operator 单独管理。
6. 每次 provider transport attempt 先写入 `running` 记录，然后启动 provider。provider error、aborted、启动失败和总时长 timeout 最多重试一次；schema 修复拥有独立的每 attempt 两次预算。第二次失败落为 `inconclusive`，不会无限依赖下一轮 observer 扫描。
7. 若任一 Judge 为 `inconclusive`、timeout、aborted 或 provider error，conclusion 直接生成确定性的 `inconclusive` 记录，不再启动另一个 provider。Discussion 子评测给出 `discussion_meaningful`、`discussion_progressed`，PRD fidelity 子评测给出 `prd_ready_for_next_stage`；主 Judge 汇总时原样保留这些判定和 Agent 分数。运行时检查类型与来源，不从分数硬编码就绪阈值；缺少子判定时不形成完整结论。
8. 每一阶段写入 sidecar、attempt sidecar 和进度事件 `benchmark.evaluation.progress`。`judge.completed` 与 `conclusion.completed` 仍是 informational/projection-only 事件。

## 超时与活性预算

保留总时长硬上限 `ZF_EVALUATION_JUDGE_TIMEOUT_S`（默认 1800 秒），另外支持：

- `ZF_EVALUATION_JUDGE_START_TIMEOUT_S`：provider 启动/readiness 上限，默认 120 秒；
- `ZF_EVALUATION_JUDGE_FIRST_PROGRESS_TIMEOUT_S`：首次有效 transcript 进展上限，默认 300 秒；
- `ZF_EVALUATION_JUDGE_IDLE_TIMEOUT_S`：当前 turn 有效 transcript 无增长上限，默认 300 秒；
- `ZF_EVALUATION_JUDGE_MAX_TOOL_CALLS`、`ZF_EVALUATION_JUDGE_MAX_SAME_TOOL_FINGERPRINT`、`ZF_EVALUATION_JUDGE_MAX_TOOL_WALL_TIME_S`：默认关闭，用于限制 runaway tool turn。

metadata、旧 turn、user/tool 文本不会刷新当前 turn 的有效进展预算。

## 报告与重测

`report.md` 是当前最新投影；每次写入前旧内容会保存为带 body digest 的 snapshot。最终报告还按 prompt version、trial 和 source snapshot digest 写入独立归档。相同 channel 的新 revision、新 trial、workflow bind 后的新 digest 都不会覆盖旧报告。旧版仅有 `report.md` 的运行也会先被保存。

报告中的状态可以是 `scored`、`evidence_missing`、`abstain`、`conflict`、`invalid`、`inconclusive` 或等待状态。`inconclusive` 会同时出现在 conclusion sidecar、Markdown missing 区域、Channel projection 和 Web Channel 页面。

observer 会检查完成后的 conclusion 和报告：结论缺失或损坏时只修复 conclusion，继续使用有效子评测；结论有效而报告缺失或内容损坏时直接重建报告，不调用 provider。结论恢复沿用持久化尝试次数，耗尽后记录 `inconclusive`，后续扫描不能无限重试。

同一冻结输入不会为了观察评分波动而自动重评。observer 重启、重新扫描、更新 role.md 或收到冻结后的新消息，都不会主动创建新的评分身份；规则快照取首次入队时的 role catalog。需要人工复核时显式调用：

```bash
zf evaluation-regrade --input-ref artifacts/benchmark-evaluations/queue-inputs/<context-digest>.json --regrade-id <id>
```

`--input-ref` 从配置的 state dir 下 `artifacts/benchmark-evaluations/scheduler.json` 对应行的 `input_ref` 取得，精确选择需要复核的冻结/Workflow 绑定版本。`--regrade-id` 可省略，届时生成新 UUID；需要重复测试时使用新 id。

手动重评分从已入队的 immutable context 读取材料，不重新扫描 Channel；首次手动执行使用当前 prompt 和 role catalog，随后将该次规则快照固化用于恢复。它使用 `artifacts/benchmark-evaluations/regrades/<id>/scheduler.json`、新的 workflow/request identity 和 `reports/regrade-<id>/report.md`，来源保存在报告旁的 `regrade.json`。同一 state dir 同时只运行一个手动重评，独占 `judges-slot-8`，与普通 slot 0～7 隔离。原报告、Judge sidecar 和 Channel 当前评测投影保持不变。相同 `regrade-id` 与同一输入幂等返回；中断后需再次手动执行恢复，同一 id 指向其他输入会失败。手动事件带 `evaluation_origin=manual_regrade`，不会进入 canonical Channel projection。

每个 Judge 结果和 attempt 记录评测配置，报告旁 `audit/<identity>/evaluation.json` 汇集各阶段及 conclusion 的审计元数据：provider/backend、transcript 可观察到的 model、timeout/tool/retry budget、prompt/system/role digest、`sample_count=1` 及配置摘要。timeout/tool budget 在 request 执行前读取，传入实际执行器，返回后不重读环境；这不是对外部 provider 隐藏配置或随机性的完整复现保证。未观察到的 model/sampling 写 `unknown`，尚未配置的 token/cost budget 写 `null`；transport/schema retry 不计为额外 sample。上述细节不写入简洁的 `report.md`。

Workflow bind 产生新的 PRD fidelity 输入后，投影按队列首次建立的顺序选择当前评测，并用原始 queue source digest 绑定对应 conclusion。旧结论及迟到事件不能把当前状态改回已完成；冻结材料未变的 contribution、discussion 结果继续复用。

Channel 只读投影可补齐同一 prompt 版本内缺少身份字段的历史事件：scheduler 行必须携带真实冻结事件、trial、source digest，且 job id 必须匹配当前 prompt 版本的请求摘要；后续事件只从同一 run/stage/trial 的唯一匹配任务补齐缺字段。conclusion 的不同 request id 仅绑定唯一活动任务；显式冲突字段仍拒绝。旧 prompt 版本的结果保留审计，不混入当前版本展示。投影回放不改写历史账本，也不启动评测。页面显示“PRD 已冻结”，当前冻结共识允许 Plan Task；旧 readiness 和 synthesis 的 open_questions 不再拦截规划，readiness 保留给实际执行检查。

## 评分质量门禁（prompt v8）

门禁位于评测后端和评测 prompt，不修改 Channel 业务状态或前端操作流程。

- 每个 `scored` 项必须给出至少一条精确匹配 packet allowlist 的非空证据引用和非空理由；集合级引用不能替代逐项证据。引用合法不代表内容支持评分，后者仍需人工校准。
- 分类器显式返回 `abstain`、`evidence_missing`、`invalid` 或 `ambiguous` 时保持非评分状态；即使同时给出明确类型和高 confidence，也不能放行质量评测。
- canonical role-driven v2 的所有质量分数统一为 0～4，且必须是有限整数。confidence 必须是 0～1 的有限数值，不接受 bool、字符串、NaN 或 Infinity。门槛判断在舍入前执行。confidence 低于 0.60 时弃权，缺证据时明确标记 `evidence_missing`；两者均保留原因并清空数字分数。当前入口没有旧阶段或旧评分合同的兼容路径。
- `discussion` 必须完整返回五个已知维度，且每个恰好一次。`prd_fidelity` 在 PRD 未绑定 Workflow 时只评 `original_to_channel_prd`，绑定后必须返回两个边界；缺项、重复或未知维度不能当作完整评分。`contribution` 按 packet 的冻结 roster 逐一评价，缺少 roster 时使用冻结消息的成员集合；不允许重复、冒名或漏评，名册本身不能证明贡献。每个 Agent 还必须给出 `must`、`optional`、`not_needed` 或证据不足时的 `undetermined` 必要性。
- `report.md` 只展示三个 subagent 的原始维度分数、逐 Agent 贡献分和必要性；不重新计算、平均或覆盖 subagent 分数。缺证据、弃权和无效结果保留原状态。
- conclusion 不得修改 Judge 的分数或补造 Agent 分数；上游不完整时不能宣告完整完成或 PRD 已就绪。
- Workflow 对齐只消费 `workflow.invoke.accepted`，按 `source_event_id` 追溯精确请求，且 `source_refs` 的 Channel/thread/revision/PRD ref/digest 以及 readiness ref/digest 必须同时匹配本次冻结；pending/rejected 或身份缺失不构成绑定。读取匹配绑定实际引用的 PRD 正文，不回退到其他 revision 或 readiness。原始需求或比较正文缺失时确定性产生 `evidence_missing`，不调用 provider。
- `channel.prd.frozen` 的 role-driven 运行使用统一 v2 的 0～4 分合同；`channel.consensus.reached` 只保留为内部冻结前事件，不再有独立评测合同。三个维度（PRD 忠实性、讨论有效性、Agent 贡献）独立展示；Agent 贡献必须同时给出 `must`、`optional`、`not_needed`，证据不足时为 `undetermined`/`incomplete`，不把贡献简单平均成团队分数。通用证据要求和对应 role.md 规则随实际 prompt sidecar 送入 provider。Codex 的精简启动 persona 不会导致 rubric 丢失。

这次 prompt 版本为当前 `prd-eval-prompts.v8`；评测身份包含 prompt 版本，新版本结果与旧报告分别保留。不合约的输出使用既有 schema 修复预算，不增加新的无限重试路径。role catalog 的版本、digest、role.md 引用和完整 packet 只保存在 audit sidecar；`report.md` 保持简洁。

三个 role.md 进一步给出每个评估区块的 0～4 分锚点和边界案例：不按发言量奖励贡献，未采纳但排除了错误方案的分析仍可计入；贡献与必要性独立判断。评审材料缺失用 `evidence_missing`，材料存在但无法可靠判断用 `abstain`；材料齐全而 PRD 漏写约束/验收属于可评分的缺陷。

机械门禁只能保证结构、证据引用和计算约束；不能证明语义评分绝对正确。以下人工校准方法尚未接入自动回放工具。

## Golden cases 人工校准

当前没有已标注标准案例，本轮不新增案例集或自动校准。以下保留为后续人工校准方法。

1. 先选三个真实冻结案例试标：高质量、有明显问题、缺证据各一个；再扩到 10～20 个，覆盖安静但有关键贡献的角色，以及 Workflow 未绑定和已绑定的场景。
2. 从配置的 `project.state_dir` 下 `artifacts/benchmark-evaluations/inputs/` 保留各阶段 packet 的副本；记录 `workflow_run_id`、`trial_id`、`source_snapshot_digest`、`prompt_version` 和文件 SHA-256。人工与模型只读同一份输入。
3. 每个 Agent、`discussion` 五维、适用 `prd_fidelity` 边界各标一项：`item_id`、`expected_status`、0～4 的整数分数（未评分填 null）、理由、证据 ref 和原文摘录。`contribution` 另标 necessity。使用当前 prompt 内的 rubric，不照抄模型分数；“缺证据”不等于“表现差”。
4. 最好两人独立盲审后裁决争议。按 Channel/任务分组，用约 2/3 案例改进 rubric，其余封存为留出集，同一任务的不同 revision 不可分到两边。
5. 对比逐维分数误差、错误给分、无故拒评、证据是否支持理由，以及同输入重复回放的稳定性。先在开发集确定验收阈值，再看留出集；保留旧标签和旧报告，记录每次回放的模型及 prompt 版本。

## 运行时入口

- CLI：`zf evaluation-watch`；`--once` 只做发现和持久化入队，不启动临时 provider pool。
- tmux：`zf start` 创建 evaluation watcher；Judge slot 由 scheduler 按需创建/复用。
- Web：`channel_projection.py` 折叠三个评估事件，Channel summary/detail/conversation 暴露 `evaluation.stages`；Channel 页面显示阶段、slot、attempt、phase、错误和 `inconclusive`。
- 事件注册：`known_types.py` 和 `event_problem_registry.py` 将评估进度与终态标为 projection-only/informational，不唤醒工作流。

## 验证边界

mock provider、scheduler、transcript monitor、runner、报告归档、事件与 Channel projection 有 focused tests。真实 Codex/Claude、真实 tmux 和浏览器 E2E 仍是显式环境测试，不在普通单元测试中启动。
