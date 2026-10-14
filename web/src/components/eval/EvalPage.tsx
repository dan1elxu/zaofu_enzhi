import { ArrowLeft, CircleDashed } from "lucide-react";
import { Fragment, useEffect, useMemo, useState } from "react";
import { getEvalRunDetail, getEvalRuns } from "../../api/client";
import type {
  EvalCrossCompare,
  EvalRunDetail,
  EvalRunGroup,
  EvalRunTrialSummary,
  RecentEvent,
} from "../../api/types";
import type { LiveState } from "../../app/sharedTypes";
import { MarkdownText } from "../agent-session/MarkdownText";
import {
  errorMessage,
  evalDimensionLabel,
  evalLaneLabel,
  evalLaneStatusLabel,
  evalLaneTone,
  evalRankingConfidenceLabel,
  evalRunStatusLabel,
  evalRunTone,
  evalScoreDimensions,
  evalTaskTypeLabel,
  formatEvalChecklist,
  formatEvalConfidence,
  formatEvalScore,
  formatEvalTimestamp,
  groupEvalKeyGaps,
  groupLaneAggregates,
  orderedEvalLanes,
  readInitialEvalRunId,
  revealCandidateLabel,
  shortEvalDigest,
  sortEvalGroups,
  sortEvalTrials,
  writeEvalRunSelection,
} from "./evalRunsModel";

const EVAL_EVENT_PREFIX = "benchmark.eval.";

export function EvalPage({
  events,
  liveState,
  projectId,
}: {
  events: RecentEvent[];
  liveState: LiveState;
  projectId: string;
}) {
  const [groups, setGroups] = useState<EvalRunGroup[] | null>(null);
  const [listError, setListError] = useState("");
  const [selectedRunId, setSelectedRunId] = useState(readInitialEvalRunId);
  const [detail, setDetail] = useState<EvalRunDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [loadedProject, setLoadedProject] = useState(projectId);
  if (loadedProject !== projectId) {
    setLoadedProject(projectId);
    setGroups(null);
    setListError("");
    setDetail(null);
    setDetailError("");
  }

  useEffect(() => {
    let cancelled = false;
    setListError("");
    if (!projectId) {
      setGroups([]);
      return undefined;
    }
    void getEvalRuns(projectId)
      .then((page) => {
        if (!cancelled) setGroups(page.groups ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setListError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, refreshTick]);

  useEffect(() => {
    if (!selectedRunId || !projectId) {
      setDetail(null);
      setDetailError("");
      return undefined;
    }
    let cancelled = false;
    setDetailError("");
    void getEvalRunDetail(selectedRunId, projectId)
      .then((value) => {
        if (!cancelled) setDetail(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) setDetailError(errorMessage(error));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, selectedRunId, refreshTick]);

  const latestEvalSeq = useMemo(() => {
    for (const event of events) {
      if (typeof event.type === "string" && event.type.startsWith(EVAL_EVENT_PREFIX)) {
        return event.seq ?? 0;
      }
    }
    return 0;
  }, [events]);

  useEffect(() => {
    if (!latestEvalSeq) return undefined;
    const timer = window.setTimeout(() => setRefreshTick((tick) => tick + 1), 600);
    return () => window.clearTimeout(timer);
  }, [latestEvalSeq]);

  const openRun = (runId: string) => {
    setSelectedRunId(runId);
    setDetail(null);
    setDetailError("");
    writeEvalRunSelection(runId);
  };
  const closeRun = () => {
    setSelectedRunId("");
    setDetail(null);
    setDetailError("");
    writeEvalRunSelection("");
  };
  const retry = () => setRefreshTick((tick) => tick + 1);

  const currentDetail = detail && detail.run_id === selectedRunId ? detail : null;

  return (
    <section className="eval-page" data-testid="eval-page">
      {selectedRunId ? (
        currentDetail ? (
          <EvalRunDetailView detail={currentDetail} onBack={closeRun} />
        ) : (
          <div className="eval-detail-pending">
            <button className="icon-button" data-testid="eval-back" type="button" onClick={closeRun}>
              <ArrowLeft size={15} strokeWidth={1.9} aria-hidden="true" /> 返回列表
            </button>
            {detailError ? (
              <div className="notice" role="alert">
                {detailError}{" "}
                <button className="icon-button" data-testid="eval-detail-retry" type="button" onClick={retry}>重试</button>
              </div>
            ) : (
              <p className="muted">正在加载 run 详情…</p>
            )}
          </div>
        )
      ) : (
        <EvalRunList
          error={listError}
          groups={groups}
          liveState={liveState}
          onOpenRun={openRun}
          onRetry={retry}
        />
      )}
    </section>
  );
}

function EvalRunList({
  error,
  groups,
  liveState,
  onOpenRun,
  onRetry,
}: {
  error: string;
  groups: EvalRunGroup[] | null;
  liveState: LiveState;
  onOpenRun: (runId: string) => void;
  onRetry: () => void;
}) {
  return (
    <div className="eval-run-list" data-testid="eval-run-list">
      <header className="eval-heading">
        <div>
          <h2>Eval 对比</h2>
          <p className="muted">三通道 PRD 生成质量对比评测。只读投影；启动与重评走 CLI（zf eval run）。</p>
        </div>
      </header>
      {liveState !== "live" ? (
        <div className="eval-stream-notice" role="status">
          实时更新通道 {liveState}；列表为最近一次成功加载的投影。
        </div>
      ) : null}
      {error ? (
        <div className="notice" role="alert">
          {error}{" "}
          <button className="icon-button" data-testid="eval-list-retry" type="button" onClick={onRetry}>重试</button>
        </div>
      ) : null}
      {groups === null ? (
        error ? null : <p className="muted">正在加载 eval run…</p>
      ) : groups.length === 0 ? (
        <div className="eval-empty">
          <CircleDashed size={20} aria-hidden="true" />
          <strong>暂无 eval run</strong>
          <span>通过 zf eval run --prompt-file &lt;path&gt; 启动一次三通道评测。</span>
        </div>
      ) : (
        sortEvalGroups(groups).map((group) => (
          <EvalRunGroupCard group={group} key={group.prompt_digest} onOpenRun={onOpenRun} />
        ))
      )}
    </div>
  );
}

function EvalRunGroupCard({
  group,
  onOpenRun,
}: {
  group: EvalRunGroup;
  onOpenRun: (runId: string) => void;
}) {
  const aggregates = groupLaneAggregates(group);
  return (
    <section className="eval-group" data-testid="eval-run-group">
      <header className="eval-group-head">
        <strong className="mono" title={group.prompt_digest}>{shortEvalDigest(group.prompt_digest)}</strong>
        <span className="muted">{group.prompt_version || "未知版本"}</span>
        <span className="muted">{group.trials?.length ?? 0} 次 trial</span>
        {aggregates.map((aggregate) => (
          <span className="eval-group-agg" key={aggregate.lane} title={`${aggregate.count} 次评分`}>
            {evalLaneLabel(aggregate.lane)} 均分 {formatEvalScore(aggregate.mean)}
            {aggregate.count > 1 ? `（${formatEvalScore(aggregate.min)}–${formatEvalScore(aggregate.max)}）` : ""}
          </span>
        ))}
      </header>
      <div className="eval-trial-table">
        {sortEvalTrials(group.trials ?? []).map((trial) => (
          <EvalTrialRow key={trial.run_id} onOpenRun={onOpenRun} trial={trial} />
        ))}
      </div>
    </section>
  );
}

function EvalTrialRow({
  onOpenRun,
  trial,
}: {
  onOpenRun: (runId: string) => void;
  trial: EvalRunTrialSummary;
}) {
  return (
    <button
      className="eval-trial-row"
      data-testid="eval-run-row"
      type="button"
      onClick={() => onOpenRun(trial.run_id)}
    >
      <span className="eval-trial-id mono">trial-{trial.trial}</span>
      <span className={`eval-status-pill tone-${evalRunTone(trial.status)}`}>
        {evalRunStatusLabel(trial.status)}
      </span>
      <span className="eval-trial-lanes">
        {orderedEvalLanes(trial.lanes).map(({ lane, state }) => (
          <span
            className={`eval-lane-mini tone-${evalLaneTone(state.status)}`}
            key={lane}
            title={state.error || undefined}
          >
            {evalLaneLabel(lane)} {evalLaneStatusLabel(state.status, state.error)}
          </span>
        ))}
      </span>
      <span className="eval-trial-scores">
        {orderedEvalLanes(trial.composite_scores ?? {}).map(({ lane, state }) => (
          <span className="mono" key={lane}>{evalLaneLabel(lane)} {formatEvalScore(state)}</span>
        ))}
      </span>
      <span className="eval-trial-rec">
        {trial.recommendation?.length ? `推荐 ${trial.recommendation.join(" > ")}` : ""}
      </span>
      <span className="muted">{formatEvalTimestamp(trial.started_at)}</span>
    </button>
  );
}

function EvalRunDetailView({
  detail,
  onBack,
}: {
  detail: EvalRunDetail;
  onBack: () => void;
}) {
  const labelMap = detail.label_map ?? {};
  const lanes = orderedEvalLanes(detail.lanes);
  return (
    <div className="eval-run-detail" data-testid="eval-run-detail">
      <header className="eval-detail-head">
        <button className="icon-button" data-testid="eval-back" type="button" onClick={onBack}>
          <ArrowLeft size={15} strokeWidth={1.9} aria-hidden="true" /> 返回列表
        </button>
        <h2>trial-{detail.trial}</h2>
        <span className={`eval-status-pill tone-${evalRunTone(detail.status)}`}>
          {evalRunStatusLabel(detail.status)}
        </span>
        <span className="mono muted">{detail.run_id}</span>
      </header>
      <section className="eval-card" data-testid="eval-prompt">
        <h3>入口提示词</h3>
        <p className="muted">
          digest <span className="mono" title={detail.prompt_digest}>{shortEvalDigest(detail.prompt_digest)}</span>
          {" · "}版本 {detail.prompt_version || "未知"}
        </p>
        <pre className="eval-prompt-text">{detail.prompt_text || ""}</pre>
      </section>
      <div className="eval-lane-strip" aria-label="lane 进度">
        {lanes.map(({ lane, state }) => (
          <div
            className={`eval-lane-chip tone-${evalLaneTone(state.status)}`}
            data-testid={`eval-lane-status-${lane}`}
            key={lane}
          >
            <strong>{evalLaneLabel(lane)}</strong>
            <span>{evalLaneStatusLabel(state.status, state.error)}</span>
            {state.error && state.error !== "timeout" ? (
              <small className="eval-lane-error" title={state.error}>{state.error}</small>
            ) : null}
            {state.finished_at ? <small>{formatEvalTimestamp(state.finished_at)}</small> : null}
          </div>
        ))}
      </div>
      <section className="eval-prd-compare">
        <h3>PRD 对比（lane 身份已揭示）</h3>
        <div className="eval-prd-grid">
          {lanes.map(({ lane, state }) => (
            <article className="eval-prd-card" data-testid={`eval-prd-${lane}`} key={lane}>
              <header>
                <strong>{evalLaneLabel(lane)}</strong>
                <span className={`eval-status-pill tone-${evalLaneTone(state.status)}`}>
                  {evalLaneStatusLabel(state.status, state.error)}
                </span>
              </header>
              {state.prd_markdown ? (
                <MarkdownText className="eval-prd-body" content={state.prd_markdown} />
              ) : (
                <p className="muted">
                  {state.status === "failed"
                    ? `未产出 PRD（${state.error || "失败"}）。`
                    : state.status === "absent"
                      ? "该 lane 未参与本次 run。"
                      : "PRD 尚未落盘。"}
                </p>
              )}
            </article>
          ))}
        </div>
      </section>
      <EvalScoreCard detail={detail} />
      <CrossCompareCard crossCompare={detail.evaluation?.cross_compare} labelMap={labelMap} />
      <section className="eval-card" data-testid="eval-conclusion">
        <h3>结论报告</h3>
        {detail.report_markdown ? (
          <MarkdownText className="eval-report-body" content={detail.report_markdown} />
        ) : (
          <p className="muted">报告将在评测完成后生成。</p>
        )}
      </section>
    </div>
  );
}

function EvalScoreCard({ detail }: { detail: EvalRunDetail }) {
  const evaluation = detail.evaluation;
  const laneScores = evaluation?.lane_scores ?? {};
  const dimensions = evalScoreDimensions(laneScores);
  const laneIds = orderedEvalLanes(detail.lanes).map(({ lane }) => lane);
  const taskType = evaluation?.task_type;
  const confidence = formatEvalConfidence(taskType?.confidence);
  return (
    <section className="eval-card" data-testid="eval-scores">
      <h3>评测评分</h3>
      {taskType ? (
        <p className="eval-task-type">
          任务类型：{evalTaskTypeLabel(taskType.task_type)}
          {confidence ? `（置信 ${confidence}）` : ""}
        </p>
      ) : (
        <p className="muted">任务分类尚未完成。</p>
      )}
      {dimensions.length ? (
        <table className="eval-score-table">
          <thead>
            <tr>
              <th>维度</th>
              {laneIds.map((lane) => <th key={lane}>{evalLaneLabel(lane)}</th>)}
            </tr>
          </thead>
          <tbody>
            {dimensions.map((dimension) => (
              <tr key={dimension}>
                <td>{evalDimensionLabel(dimension)}</td>
                {laneIds.map((lane) => (
                  <td className="mono" key={lane}>
                    {formatEvalScore(laneScores[lane]?.dimensions?.[dimension])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>综合参考分</td>
              {laneIds.map((lane) => (
                <td className="mono" key={lane}>{formatEvalScore(laneScores[lane]?.composite)}</td>
              ))}
            </tr>
          </tfoot>
        </table>
      ) : (
        <p className="muted">各 lane 单项评分尚未完成。</p>
      )}
      {laneIds.map((lane) => {
        const checklist = formatEvalChecklist(laneScores[lane]?.checklist);
        if (!checklist) return null;
        return (
          <p className="eval-lane-checklist" data-testid={`eval-checklist-${lane}`} key={lane}>
            {evalLaneLabel(lane)} {checklist}
          </p>
        );
      })}
      {laneIds.map((lane) => {
        const score = laneScores[lane];
        if (!score || score.status === "scored") return null;
        return (
          <p className="eval-lane-score-status muted" key={lane}>
            {evalLaneLabel(lane)} 评分状态：{evalLaneStatusLabel(score.status)}
          </p>
        );
      })}
    </section>
  );
}

function CrossCompareCard({
  crossCompare,
  labelMap,
}: {
  crossCompare: EvalCrossCompare | null | undefined;
  labelMap: Record<string, string>;
}) {
  if (!crossCompare) return null;
  const recommendation = crossCompare.recommendation ?? [];
  const rankings = crossCompare.dimension_rankings ?? [];
  const gapGroups = groupEvalKeyGaps(crossCompare.key_gaps, labelMap);
  const conflicts = crossCompare.conflicts ?? [];
  const reveal = (label: unknown) => revealCandidateLabel(label, labelMap);
  return (
    <section className="eval-card" data-testid="eval-cross-compare">
      <div className="eval-card-head">
        <h3>横向对比</h3>
        <span className={`eval-status-pill tone-${evalLaneTone(crossCompare.status)}`}>
          {evalLaneStatusLabel(crossCompare.status)}
        </span>
      </div>
      <h4 className="eval-sub-head">推荐排名</h4>
      {recommendation.length ? (
        <ol className="eval-rec-ranking" data-testid="eval-rec-ranking">
          {recommendation.map((label, index) => (
            <li className="eval-rec-rank" key={`${String(label)}-${index}`}>
              <span className={`eval-rank-medal eval-rank-${index + 1}`} aria-label={`第 ${index + 1} 名`}>
                {index + 1}
              </span>
              <span className="eval-lane-tag">{reveal(label)}</span>
              {labelMap[label] ? <span className="muted">（{label}）</span> : null}
            </li>
          ))}
        </ol>
      ) : (
        <p className="muted">暂无推荐排名。</p>
      )}
      {rankings.length ? (
        <>
          <h4 className="eval-sub-head">维度排名</h4>
          <div className="eval-table-scroll">
            <table className="eval-dim-table" data-testid="eval-dim-table">
              <thead>
                <tr>
                  <th>维度</th>
                  <th>第 1 名</th>
                  <th>第 2 名</th>
                  <th>第 3 名</th>
                  <th>置信度</th>
                </tr>
              </thead>
              <tbody>
                {rankings.map((entry, index) => {
                  const confidence = evalRankingConfidenceLabel(entry.confidence);
                  return (
                    <Fragment key={entry.dimension_id || index}>
                      <tr data-testid={`eval-dim-row-${entry.dimension_id || index}`}>
                        <td>{evalDimensionLabel(entry.dimension_id)}</td>
                        {[0, 1, 2].map((place) => {
                          const label = entry.ranking?.[place];
                          return (
                            <td key={place}>
                              {label ? (
                                <span className="eval-lane-tag">{reveal(label)}</span>
                              ) : (
                                <span className="muted">—</span>
                              )}
                            </td>
                          );
                        })}
                        <td>{confidence || "—"}</td>
                      </tr>
                      {entry.rationale ? (
                        <tr className="eval-dim-rationale-row">
                          <td colSpan={5}>
                            <details className="eval-dim-rationale">
                              <summary>判定理由</summary>
                              <p>{entry.rationale}</p>
                            </details>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
      {gapGroups.length ? (
        <div className="eval-gap-groups" data-testid="eval-gap-groups">
          <h4 className="eval-sub-head">关键缺口</h4>
          {gapGroups.map((group) => (
            <section className="eval-gap-group" data-testid={`eval-gap-group-${group.lane}`} key={group.lane}>
              <h5 className="eval-gap-group-title">
                {group.title}
                {group.blindLabel ? <span className="muted">（{group.blindLabel}）</span> : null}
              </h5>
              <ul className="eval-gap-list">
                {group.gaps.map((gap, index) => (
                  <li key={index}>{gap.gap}</li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      ) : null}
      {conflicts.length ? (
        <div className="eval-conflict-section" data-testid="eval-conflicts">
          <h4 className="eval-sub-head">评分冲突</h4>
          <ul className="eval-conflicts">
            {conflicts.map((conflict, index) => (
              <li key={index}>
                {evalDimensionLabel(conflict.dimension_id)}：blind 分偏好 {reveal(conflict.blind_favorite)}，
                逐段对比偏好 {reveal(conflict.pairwise_favorite)} — {conflict.resolution}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
