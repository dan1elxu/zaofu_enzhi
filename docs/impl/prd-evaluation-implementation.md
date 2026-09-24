# PRD Evaluation 实施说明

本实现把 Kanban/Channel 的冻结快照送入一个持久化、有界并发的 Judge 队列。评估事件是只读投影事件，不改变任务、Channel 或 PRD 的权威状态。

## 流程

1. `evaluation_watch` 读取 `events.jsonl`，发现 `channel.consensus.reached` 或 `channel.result.receipt.recorded(prd_confirmed)`。
2. observer 在冻结事件处截断消息，读取冻结 PRD/readiness，生成不可变 packet。packet 写入 `artifacts/benchmark-evaluations/queue-inputs/`，其中包含 `workflow_run_id`、`trial_id`、`judge_type` 和 `source_snapshot_digest`。
3. 四个阶段进入 `evaluation_scheduler.py`：`milestone → communication → prd_result → conclusion`。同一 channel 严格按依赖执行；不同 channel 可以占用不同 slot。默认 2 个 slot，可用 `ZF_EVALUATION_JUDGE_SLOTS` 调整（上限 8）。
4. 每个 slot 使用独立的 `judges-slot-N` tmux window、provider launch UUID、Codex `CODEX_HOME`/session root 和 binding sidecar。observer 只发现并入队，不等待 provider。
5. Judge turn 通过 `evaluation_turns.py` 按当前 provider session、rollout 和 turn 过滤 transcript。Codex 必须匹配本次 prompt marker；不会按项目目录“最新 rollout”猜测。Claude 只把当前 user marker 后的 assistant 内容作为结果。
6. 每次 provider transport attempt 先写入 `running` 记录，然后启动 provider。provider error、aborted、启动失败和总时长 timeout 最多重试一次；schema 修复拥有独立的每 attempt 两次预算。第二次失败落为 `inconclusive`，不会无限依赖下一轮 observer 扫描。
7. 若任一 Judge 为 `inconclusive`、timeout、aborted 或 provider error，conclusion 直接生成确定性的 `inconclusive` 记录，不再启动另一个 provider。若三个 Judge 都有终态结果，conclusion provider 只负责文字结论，分数由 runner 确定性重算。
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

报告中的状态可以是 `scored`、`evidence_missing`、`abstain`、`invalid`、`inconclusive` 或等待状态。`inconclusive` 会同时出现在 conclusion sidecar、Markdown missing 区域、Channel projection 和 Web Channel 页面。

## 运行时入口

- CLI：`zf evaluation-watch`；`--once` 只做发现和持久化入队，不启动临时 provider pool。
- tmux：`zf start` 创建 evaluation watcher；Judge slot 由 scheduler 按需创建/复用。
- Web：`channel_projection.py` 折叠三个评估事件，Channel summary/detail/conversation 暴露 `evaluation.stages`；Channel 页面显示阶段、slot、attempt、phase、错误和 `inconclusive`。
- 事件注册：`known_types.py` 和 `event_problem_registry.py` 将评估进度与终态标为 projection-only/informational，不唤醒工作流。

## 验证边界

mock provider、scheduler、transcript monitor、runner、报告归档、事件与 Channel projection 有 focused tests。真实 Codex/Claude、真实 tmux 和浏览器 E2E 仍是显式环境测试，不在普通单元测试中启动。
