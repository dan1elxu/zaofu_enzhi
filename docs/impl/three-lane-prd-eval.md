# 三通道 PRD 对比评测实施说明

本文档描述 `docs/ideas/three-lane-prd-eval.md` 的落地实现（2026-10-14，Phase A/B 完成）。上游单 Channel 评测内核见 `prd-evaluation-implementation.md`，本文只记 eval-run 侧的新增与边界。

## 数据流

1. `zf eval run --prompt-file <path> [--n N] [--lanes terminal,kanban,channel]`（`src/zf/cli/eval.py`）调用 `run_eval`（`src/zf/runtime/eval_driver.py`）。
2. 提示词固化为不可变 packet，身份 = `prompt_digest(text, prompt_version)`；run-group 目录 `<state_dir>/artifacts/benchmark-evaluations/runs/<digest>/`，每个 trial 一个 `trial-<n>/` 目录，trial 目录同时充当本 run 的评测 state_dir（scheduler 队列、packets、judge sidecar、report 全部嵌在其下）。
3. 三条 lane 在驱动器进程内并行（`src/zf/runtime/eval_lanes.py::run_lane`）：terminal 直接跑 headless provider CLI，kanban 走 `KanbanHeadlessAgent`，channel 在 lane 私有 `.zf` 账本里手动泵 channel auto 流水线直到 `channel.prd.frozen`（`zf start` 不需要——泵重放生产 reactor 函数）。lane contract 统一为 `lanes/<lane>/prd/prd.md` 落盘。channel lane 支持 `--channel-state shared`（`zf eval run`）：channel 直接建在工作区账本里，Web Channels 页面可实时围观讨论；共享账皮下泵按 channel_id 精确过滤事件，channel id 按 trial 区分（`ch-eval-prd-t<N>`）。
4. lane 终态写入 `run.json` manifest（`src/zf/runtime/eval_run.py`，`benchmark.eval.run.v1` schema，fcntl 锁串行），生命周期事件 `benchmark.eval.run.started` / `benchmark.eval.lane.completed` / `benchmark.eval.run.completed` 写主账本，均为 informational / projection-only，不唤醒 Orchestrator。
5. lane 全部终态后进入评测：`task_type`（每 run 一次）→ `lane_prd_fidelity`（每个成功 lane 一份 blind 评分，packet 剥离 lane 身份，候选标签由 `assign_candidate_labels` 种子洗牌）→ `cross_compare`（独立 judge type，读三份 PRD + blind 分摘要）。job 入队到 trial 目录的 `EvaluationScheduler`，`evaluation_request_id` 携带 per-stage prompt 版本（当前 `lane-prd-eval-prompts.v2` / `cross-compare-prompts.v2`，与 Channel 的 `prd-eval-prompts.v8` 解耦）。v2 起 blind 评分为扣分制 + 需求清单核对：judge 先从提示词抽取编号需求清单并逐项给 pass/partial/missing（附证据引用），维度分从 4 按证据扣分，满分必须简述核查过程；cross_compare 全部结论文本用中文。
6. 结论为确定性汇总（无 provider 调用）：`conclusion.json`（`benchmark.eval.run.conclusion.v1`）+ `labels.json`（label↔lane 映射，供 Web 揭示身份）+ `render_report` 输出 run 级 `reports/<eval_run_id>/report.md` 与每 lane 报告。
7. 幂等：`(prompt_digest, trial)` 已完成直接跳过；中断重入靠 manifest 终态 + scheduler/packet 身份去重续跑，provider 不重跑。已知边界：channel lane 泵中途崩溃后同目录重建会失败，恢复手段是开新 trial。

## 批量数据集

`scripts/eval-batch.py`：JSONL（每行 `{"prompt"}` 或 `{"prompt_file"}`）× `--n` trial，`--max-concurrent` 限流，按 `(digest, trial)` 跳过已完成，单 prompt 失败不中断，结尾打印跨 prompt 汇总表。不新增 kernel 批量状态机。

## Web 投影

- API：`src/zf/web/eval_routes.py`，project-scoped 只读。`GET /api/projects/{project_id}/eval/runs`（`eval.runs.v1`，按 prompt digest 分组的 trial 摘要）与 `GET /api/projects/{project_id}/eval/runs/{run_id}`（`eval.run.v1`，提示词 / lane 状态与 PRD 正文 / 评测进度 / cross_compare / label_map / report markdown）。PRD 与 packet 均做 sha256 绑定校验，篡改或越界返回 null/404，不猜测；报告按 `reports/<run_id>/` 身份绑定读取（per-lane 报告同目录共存不混淆）。
- 前端：`?page=eval`（深链 `&run_id=<id>`），`web/src/components/eval/EvalPage.tsx` + `evalRunsModel.ts` + `styles/28-eval.css`。SSE 监听 `benchmark.eval.*` 前缀刷新；label→lane 身份只在详情页对人揭示，judge 侧始终保持 blind。

## 测试与验证

- 后端：`tests/test_eval_run.py`（33）、`tests/test_eval_lanes.py`（8）、`tests/test_evaluation_lane_judges.py`（46）、`tests/test_eval_driver.py`（9，fake provider 端到端）、`tests/test_eval_routes.py`（13）。回归：`tests/test_evaluation_*.py` 家族 368 项全绿；`scripts/dev-premerge-gate.sh` 全绿。
- 前端：typecheck / 单元测试 / 构建全绿；`web/tests/eval-page.spec.ts`（Playwright，`ZF_EVAL_E2E_PROJECT_ID` gating）。
- 真实 provider 冒烟（2026-10-14，codex CLI，/tmp 隔离 state + 8015 端口 Web）已完成：三 lane 全链路自动跑通（channel lane 无 `zf start` 自动冻结），v2 评分 + cross_compare + 前端页面全部真实可用。冒烟暴露并修复两个真实缺陷：cross_compare 冲突门禁误杀额外仲裁记录（`e05febf` 前）、schema 修复重试回退丢字段（修复 `7a27533`）。经验：blind 单项分在高质量 PRD 上会饱和（全 4.0 属正常），区分度主要来自 cross_compare pairwise 投票 + 多 trial 排名稳定性；judge 跨 trial 存在方差，结论必须看 N 次分布。
