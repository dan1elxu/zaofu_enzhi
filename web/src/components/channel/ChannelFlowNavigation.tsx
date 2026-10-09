import { Fragment, useEffect, useRef } from "react";
import { Check } from "lucide-react";
import { FLOW_LABELS, flowText, type ChannelFlow, type ChannelFlowNode } from "./channelFlow";
import { previewItemsFromRefs } from "../agent-session/previewRegistry";

export function ChannelFlowNavigation({ flow, onSelect, onReviewQuestions, onReviewPlans, deliveryError }: {
  flow: ChannelFlow; onSelect: (node: ChannelFlowNode) => void;
  onReviewQuestions: () => void; onReviewPlans: () => void; deliveryError?: string;
}) {
  const nodes = Object.keys(FLOW_LABELS) as ChannelFlowNode[];
  const navigation = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navigation.current;
    const current = nav?.querySelector<HTMLElement>('[aria-current="step"]')?.closest<HTMLElement>(".channel-flow-node");
    if (!nav || !current) return;
    const revealCurrent = () => {
      const stageBounds = current.getBoundingClientRect();
      const navBounds = nav.getBoundingClientRect();
      if (stageBounds.left < navBounds.left) nav.scrollLeft += stageBounds.left - navBounds.left - 10;
      else if (stageBounds.right > navBounds.right) nav.scrollLeft += stageBounds.right - navBounds.right + 10;
    };
    revealCurrent();
    const resize = new ResizeObserver(revealCurrent);
    resize.observe(nav);
    resize.observe(current);
    return () => resize.disconnect();
  }, [flow.current]);
  const questions = flow.questionCount;
  return <section className="channel-flow" aria-label="Delivery progress">
    <nav ref={navigation} className="channel-flow-nav" aria-label="Brief to Workflow" data-testid="channel-flow-nav">
    {nodes.map((node, index) => {
      const status = flow.statuses[node];
      const needsAttention = ["Needs attention", "Needs revision", "Failed", "Paused"].includes(status);
      const showStatus = needsAttention || ["Awaiting input", "Awaiting approval"].includes(status);
      const hasPlan = (node === "task" || node === "workflow") && flow.plans.some((plan) =>
        plan.subject_type === (node === "task" ? "task_create" : "task_workflow"));
      return <Fragment key={node}><div className="channel-flow-node" data-node={node}>
      <button type="button" aria-current={flow.current === node ? "step" : undefined}
        className={`channel-flow-node-button ${flow.complete[node] ? "is-complete" : ""}`}
        data-testid={`channel-flow-${node}`} title={`${FLOW_LABELS[node]} · ${flow.statuses[node]}`}
        aria-label={`${FLOW_LABELS[node]} · ${flow.statuses[node]}`}
        onClick={() => onSelect(node)}>
        <span className="channel-flow-marker" aria-hidden="true">{flow.complete[node] ? <Check size={12} /> : index + 1}</span>
        <strong>{FLOW_LABELS[node]}</strong>
      </button>
      {!flow.loading ? node === "discussion" && questions > 0 ? <button type="button" className="channel-flow-badge channel-flow-action"
        data-testid="channel-flow-next" aria-label={`${questions} ${questions === 1 ? "decision" : "decisions"}`}
        title={`Locate ${questions} ${questions === 1 ? "decision" : "decisions"}`} onClick={onReviewQuestions}>
        {questions}
      </button> : hasPlan ? <button type="button" className={`channel-flow-badge channel-flow-action ${needsAttention ? "needs-attention" : ""}`}
        data-testid={`channel-flow-plan-${node}`} aria-label="Open plan" title={`${status} · Open plan`} onClick={onReviewPlans}>
        {showStatus ? status : "Open plan"}
      </button> : showStatus ? <span className={`channel-flow-badge ${needsAttention ? "needs-attention" : ""}`}>{status}</span> : null
      : null}
    </div>
    {index < nodes.length - 1 ? <span className="channel-flow-connector" aria-hidden="true"><span className="channel-flow-line" /></span> : null}
    </Fragment>;
    })}
    </nav>
    {deliveryError ? <p className="channel-flow-error" role="status">Delivery status unavailable. Reopen the Channel to retry.</p> : null}
  </section>;
}

export function ChannelFlowPreview({ flow, node }: { flow: ChannelFlow; node: ChannelFlowNode }) {
  function taskHref(taskId: string) {
    const params = new URLSearchParams(window.location.search);
    params.set("page", "task");
    params.set("task", taskId);
    params.delete("channel");
    return `/?${params}`;
  }
  if (node === "requirement") return <section className="channel-flow-preview">
    <h4>Brief · {flow.statuses.requirement}</h4>
    <p className="muted">当前线程已加载的需求与补充；更早的记录可继续加载。</p>
    {!flow.requirements.length ? <p>尚未记录需求。可在聊天中输入需求或附加文件。</p> : null}
    {flow.requirements.map((row, index) => {
      const refs = row.refs as Record<string, unknown> | undefined;
      return <article key={flowText(row.message_id) || index}>
        <p className="channel-flow-body">{flowText(row.text) || flowText(row.content) || "附件输入"}</p>
        {previewItemsFromRefs(refs).map((item) => <code key={`${item.kind}-${item.id}`}>{item.name}</code>)}
      </article>;
    })}
  </section>;
  if (node === "prd") return <section className="channel-flow-preview" data-testid="channel-prd-preview">
    <h4>{flow.statuses.prd}</h4>
    <p>{flow.hasArtifact ? flow.ready ? "可进行交付规划，执行仍需独立确认。" : "尚未满足定稿条件，请继续讨论或确认。" : "尚无 PRD 产物。讨论收敛后由主持人综合。"}</p>
    {flow.hasArtifact ? <>
      <code>{flow.artifactRef}</code>
      <details><summary>产物版本与校验信息</summary><code>{flow.artifactDigest || "未提供 digest"}</code></details>
      {flowText(flow.latest?.summary) ? <p className="channel-flow-body">{flowText(flow.latest?.summary)}</p> : null}
      <p>{flow.openQuestions.length} 项未决问题 · {flow.tasks.length} 个关联 Task</p>
    </> : null}
  </section>;
  const rows = node === "task" ? flow.tasks : flow.workflows;
  return <section className="channel-flow-preview">
    <h4>{FLOW_LABELS[node]} · {flow.statuses[node]}</h4>
    <p className="muted">仅展示当前线程、当前 PRD 版本关联的记录。Task 创建与 Workflow 启动分别审批。</p>
    {!rows.length ? <p>{node === "task" ? "尚无已创建 Task 的回执。" : "尚未收到关联执行记录；不代表已启动或已完成。"}</p> : null}
    {rows.map((row, index) => <article key={flowText(row.receipt_id) || flowText(row.event_id) || index}>
      <a href={taskHref(flowText(row.task_id))}>{flowText(row.task_id)}</a>
      <span>{flowText(row.receipt_kind) || flowText(row.type)} · {flowText(row.status)}</span>
      {flowText(row.workflow_run_id) ? <code>{flowText(row.workflow_run_id)}</code> : null}
    </article>)}
  </section>;
}
