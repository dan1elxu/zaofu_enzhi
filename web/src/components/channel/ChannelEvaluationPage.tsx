import { AlertTriangle, CheckCircle2, CircleDashed, Loader2 } from "lucide-react";
import type { ChannelEvaluationDetail, ChannelEvaluationStage } from "../../api/types";
import { MarkdownText } from "../agent-session/MarkdownText";
import {
  evaluationStageLabel,
  evaluationStatusLabel,
  evaluationDimensionLabel,
  evaluationPhaseLabel,
  evaluationScore,
  evaluationTaskTypeLabel,
  evaluationResultUnavailable,
  necessityLabel,
  orderedEvaluationStages,
  stageTone,
} from "./channelEvaluationModel";

export function ChannelEvaluationPage({ evaluation, loading, error, onRetry }: {
  evaluation: ChannelEvaluationDetail | null;
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
}) {
  if (loading && !evaluation) return <section className="channel-evaluation-page" data-testid="channel-evaluation-page"><p className="muted">正在加载评测…</p></section>;
  if (error && !evaluation) return <section className="channel-evaluation-page" data-testid="channel-evaluation-page"><div className="notice">{error} {onRetry ? <button className="icon-button" data-testid="channel-evaluation-retry" type="button" onClick={onRetry}>重试</button> : null}</div></section>;
  if (!evaluation || evaluation.status === "unavailable") {
    return <section className="channel-evaluation-page" data-testid="channel-evaluation-page">{error ? <div className="notice">{error} {onRetry ? <button className="icon-button" data-testid="channel-evaluation-retry" type="button" onClick={onRetry}>重试</button> : null}</div> : null}<div className="channel-evaluation-empty"><CircleDashed size={20} /><strong>暂无评测</strong><span>PRD 冻结后将生成五阶段评测。</span></div></section>;
  }
  const stages = orderedEvaluationStages(evaluation.stages || []);
  const contribution = stages.find((stage) => stage.judge_type === "contribution");
  const discussion = stages.find((stage) => stage.judge_type === "discussion");
  const fidelity = stages.find((stage) => stage.judge_type === "prd_fidelity");
  const conclusion = stages.find((stage) => stage.judge_type === "conclusion");
  const taskType = stages.find((stage) => stage.judge_type === "task_type")?.result?.task_type;
  return (
    <section className="channel-evaluation-page" data-testid="channel-evaluation-page">
      <div className="channel-evaluation-head">
        <div><h2>PRD 评测</h2><span className="muted">{taskType ? `任务类型：${evaluationTaskTypeLabel(taskType)}` : "当前冻结"}</span></div>
        <span className={`channel-evaluation-status tone-${stageTone(evaluation.status)}`}>{evaluationStatusLabel(evaluation.status)}</span>
      </div>
      {error ? <div className="notice">{error} {onRetry ? <button className="icon-button" data-testid="channel-evaluation-retry" type="button" onClick={onRetry}>重试</button> : null}</div> : null}
      <div className="channel-evaluation-stage-rail" aria-label="评测进度">
        {stages.map((stage) => <StageNode key={stage.judge_type} stage={stage} />)}
      </div>
      <div className="channel-evaluation-grid">
        <ScoreCard title="PRD 忠实度" stage={fidelity} />
        <ScoreCard title="讨论有效性" stage={discussion} />
      </div>
      <ContributionCard stage={contribution} />
      <ConclusionCard stage={conclusion} />
      {evaluation.report.available && evaluation.report.markdown ? (
        <details className="channel-evaluation-report">
          <summary>完整报告{evaluation.report.truncated ? "（已截断）" : ""}</summary>
          <MarkdownText content={evaluation.report.markdown} />
        </details>
      ) : <div className="channel-evaluation-empty small">{["complete", "incomplete"].includes(evaluation.status) ? "当前评测报告不可用。" : "报告将在评测完成后生成。"}</div>}
    </section>
  );
}

function StageNode({ stage }: { stage: ChannelEvaluationStage }) {
  const tone = stageTone(stage.status || stage.verdict);
  const Icon = tone === "complete" ? CheckCircle2 : tone === "warning" ? AlertTriangle : tone === "active" ? Loader2 : CircleDashed;
  return <div className={`channel-evaluation-stage tone-${tone}`}><Icon size={16} className={tone === "active" ? "spin" : ""} /><strong>{evaluationStageLabel(stage)}</strong><span>{evaluationResultUnavailable(stage) ? "结果文件不可用" : evaluationStatusLabel(stage.status || stage.verdict)}</span>{stage.phase ? <small>阶段：{evaluationPhaseLabel(stage.phase)}</small> : null}{stage.attempt ? <small>尝试 {stage.attempt}/{stage.max_attempts ?? 2}</small> : null}{stage.updated_at ? <small>{new Date(stage.updated_at).toLocaleTimeString()}</small> : null}{stage.error ? <small className="channel-evaluation-error">{stage.error}</small> : null}</div>;
}

function ScoreCard({ title, stage }: { title: string; stage?: ChannelEvaluationStage }) {
  const dimensions = stage?.result?.dimensions || [];
  return <section className="channel-evaluation-card"><div className="channel-evaluation-card-head"><h3>{title}</h3><span>{evaluationResultUnavailable(stage) ? "结果文件不可用" : evaluationStatusLabel(stage?.status || "unavailable")}</span></div>{evaluationResultUnavailable(stage) ? <p className="muted">评测已结束，但结果文件不可用。</p> : dimensions.length ? <div className="channel-evaluation-dimensions">{dimensions.map((dimension) => <div key={dimension.dimension_id}><strong>{evaluationDimensionLabel(dimension.dimension_id)}</strong><b>{evaluationScore(dimension.score, dimension.status || stage?.status)}</b><span>{dimension.verdict || ""}{dimension.rationale ? `：${dimension.rationale}` : ""}</span></div>)}</div> : <p className="muted">暂无可用维度结果。</p>}</section>;
}

function ContributionCard({ stage }: { stage?: ChannelEvaluationStage }) {
  const agents = stage?.result?.agents || [];
  return <section className="channel-evaluation-card"><div className="channel-evaluation-card-head"><h3>Agent 贡献</h3><span>{evaluationResultUnavailable(stage) ? "结果文件不可用" : evaluationStatusLabel(stage?.status || "unavailable")}</span></div>{evaluationResultUnavailable(stage) ? <p className="muted">评测已结束，但结果文件不可用。</p> : agents.length ? <div className="channel-evaluation-agents">{agents.map((agent) => <div className="channel-evaluation-agent" key={agent.agent_id}><div><strong>{agent.agent_name || agent.agent_id || "Agent"}</strong><span>{agent.role_summary || "暂无作用说明"}</span>{agent.rationale ? <span>理由：{agent.rationale}</span> : null}</div><b>{evaluationScore(agent.score, agent.status || stage?.status)}</b><em>{necessityLabel(agent.necessity)}</em></div>)}</div> : <p className="muted">暂无 Agent 贡献结果。</p>}</section>;
}

function ConclusionCard({ stage }: { stage?: ChannelEvaluationStage }) {
  const result = stage?.result;
  return <section className="channel-evaluation-card"><div className="channel-evaluation-card-head"><h3>汇总结论</h3><span>{evaluationResultUnavailable(stage) ? "结果文件不可用" : evaluationStatusLabel(stage?.status || "unavailable")}</span></div>{evaluationResultUnavailable(stage) ? <p className="muted">评测已结束，但结果文件不可用。</p> : <><p>{result?.conclusion_zh || "暂无汇总结论。"}</p>{result?.next_actions?.length ? <ul>{result.next_actions.map((action) => <li key={action}>{action}</li>)}</ul> : null}</>}</section>;
}
