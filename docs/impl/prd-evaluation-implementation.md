# PRD Evaluation 实施说明

本实现把 Kanban/Channel 的冻结快照送入一个持久化、有界并发的 Judge 队列。评估事件是只读投影事件，不改变任务、Channel 或 PRD 的权威状态。

## 流程

1. `evaluation_watch` 读取 `events.jsonl`，发现 `channel.consensus.reached` 或 `channel.result.receipt.recorded(prd_confirmed)`。
2. observer 在冻结事件处截断消息，读取冻结 PRD/readiness，生成不可变 packet。packet 写入 `artifacts/benchmark-evaluations/queue-inputs/`，其中包含 `workflow_run_id`、`trial_id`、`judge_type` 和 `source_snapshot_digest`。
3. 四个阶段进入 `evaluation_scheduler.py`：`milestone → communication → prd_result → conclusion`。同一 channel 严格按依赖执行；不同 channel 可以占用不同 slot。默认 2 个 slot，可用 `ZF_EVALUATION_JUDGE_SLOTS` 调整（上限 8）。
4. 每个 slot 使用独立的 `judges-slot-N` tmux window、provider launch UUID、Codex `CODEX_HOME`/session root 和 binding sidecar。observer 只发现并入队，不等待 provider。
5. Judge turn 通过 `evaluation_turns.py` 按当前 provider session、rollout 和 turn 过滤 transcript。Codex 必须匹配本次 prompt marker；不会按项目目录“最新 rollout”猜测。Claude 只把当前 user marker 后的 assistant 内容作为结果。
6. 每次 provider transport attempt 先写入 `running` 记录，然后启动 provider。provider error、aborted、启动失败和总时长 timeout 最多重试一次；schema 修复拥有独立的每 attempt 两次预算。第二次失败落为 `inconclusive`，不会无限依赖下一轮 observer 扫描。
7. 若任一 Judge 为 `inconclusive`、timeout、aborted 或 provider error，conclusion 直接生成确定性的 `inconclusive` 记录，不再启动另一个 provider。conclusion provider 只负责文字结论，分数、Agent 分数和完整性由 runner 根据上游结果确定，不能用文字结论掩盖缺证据或评分冲突。
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

## 评分质量门禁（prompt v5）

门禁位于评测后端和评测 prompt，不修改 Channel 业务状态或前端操作流程。

- 每个 `scored` 项必须给出至少一条精确匹配 packet allowlist 的非空证据引用和非空理由；集合级引用不能替代逐项证据。引用合法不代表内容支持评分，后者仍需人工校准。
- 分数必须是 1～5 的有限整数；confidence 必须是 0～1 的有限数值，不接受 bool、字符串、NaN 或 Infinity。门槛判断在舍入前执行。confidence 低于 0.60 时弃权，缺证据时明确标记 `evidence_missing`；两者均保留原因并清空数字分数。
- communication 必须完整返回五个已知维度，且每个恰好一次。PRD 未绑定 Workflow 时只评 `original_to_channel_prd`，绑定后必须返回两个边界；缺项、重复或未知维度不能当作完整评分。Milestone 按 packet 的冻结 roster 逐一评价，缺少 roster 时使用冻结消息的成员集合；不允许重复、冒名或漏评，名册本身不能证明贡献。
- 汇总只使用当前阶段全部必需的有效分数。缺证据、弃权和无效结果不能用剩余维度的均分冒充完整分数，报告遵守同样的规则。
- conclusion 不得修改 Judge 的分数或补造 Agent 分数；上游不完整时不能宣告完整完成或 PRD 已就绪。
- Workflow 对齐读取绑定事件实际引用的 PRD 正文，包括显式 `source_refs`；接受事件可按 `source_event_id` 追溯对应请求。不再用绑定事件元数据冒充正文，也不回退到 Channel PRD。原始需求或比较正文缺失时确定性产生 `evidence_missing`，不调用 provider。
- 通用 1～5 分 rubric、逐维锚点及证据要求直接拼入三个 Judge 的 `STAGE_PROMPTS`，随实际 prompt sidecar 送入 provider。Codex 的精简启动 persona 不会导致 rubric 丢失。

这次 prompt 版本为 `prd-eval-prompts.v5`；评测身份包含 prompt 版本，新版本结果与旧报告分别保留。不合约的输出使用既有 schema 修复预算，不增加新的无限重试路径。

机械门禁只能保证结构、证据引用和计算约束；不能证明语义评分绝对正确。以下人工校准方法尚未接入自动回放工具。

## Golden cases 人工校准

1. 先选三个真实冻结案例试标：高质量、有明显问题、缺证据各一个；再扩到 10～20 个，覆盖安静但有关键贡献的角色，以及 Workflow 未绑定和已绑定的场景。
2. 从配置的 `project.state_dir` 下 `artifacts/benchmark-evaluations/inputs/` 保留各阶段 packet 的副本；记录 `workflow_run_id`、`trial_id`、`source_snapshot_digest`、`prompt_version` 和文件 SHA-256。人工与模型只读同一份输入。
3. 每个 Agent、communication 五维、适用 PRD 边界各标一项：`item_id`、`expected_status`、1～5 的整数分数（未评分填 null）、理由、证据 ref 和原文摘录。Milestone 另标 necessity。使用当前 prompt 内的 rubric，不照抄模型分数；“缺证据”不等于“表现差”。
4. 最好两人独立盲审后裁决争议。按 Channel/任务分组，用约 2/3 案例改进 rubric，其余封存为留出集，同一任务的不同 revision 不可分到两边。
5. 对比逐维分数误差、错误给分、无故拒评、证据是否支持理由，以及同输入重复回放的稳定性。先在开发集确定验收阈值，再看留出集；保留旧标签和旧报告，记录每次回放的模型及 prompt 版本。

## 运行时入口

- CLI：`zf evaluation-watch`；`--once` 只做发现和持久化入队，不启动临时 provider pool。
- tmux：`zf start` 创建 evaluation watcher；Judge slot 由 scheduler 按需创建/复用。
- Web：`channel_projection.py` 折叠三个评估事件，Channel summary/detail/conversation 暴露 `evaluation.stages`；Channel 页面显示阶段、slot、attempt、phase、错误和 `inconclusive`。
- 事件注册：`known_types.py` 和 `event_problem_registry.py` 将评估进度与终态标为 projection-only/informational，不唤醒工作流。

## 验证边界

mock provider、scheduler、transcript monitor、runner、报告归档、事件与 Channel projection 有 focused tests。真实 Codex/Claude、真实 tmux 和浏览器 E2E 仍是显式环境测试，不在普通单元测试中启动。
