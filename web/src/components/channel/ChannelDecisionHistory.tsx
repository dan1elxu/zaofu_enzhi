import { useState } from "react";
import { DECISION_CATEGORIES, type DecisionGroup } from "./channelDecisions";

export function ChannelDecisionHistory({ groups, recordCount }: { groups: DecisionGroup[]; recordCount: number }) {
  const [open, setOpen] = useState(false);
  if (!groups.length) return null;
  return <details className="channel-decision-history" data-testid="channel-decision-history" open={open}
    onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>决定与澄清 · {groups.length}项 <small>{recordCount} 条原始记录</small></summary>
    {open ? <div className="channel-decision-history-body">
      <p className="muted">分类依据记录类型或答复中的明确标注；测试假设仅适用于测试。</p>
      {Object.entries(DECISION_CATEGORIES).map(([category, label]) => {
        const rows = groups.filter((group) => group.category === category);
        if (!rows.length) return null;
        return <section key={category} data-decision-category={category} aria-label={label}>
          <h4>{label} · {rows.length}</h4>
          {rows.map((group) => {
            const first = group.receipts[0];
            return <details className="channel-decision-group" data-testid="channel-decision-group" key={group.id}>
              <summary><span>{first.question || first.answer || first.resolution}</span>
                <small>{group.receipts.length} 条原始记录</small></summary>
              {group.receipts.length > 1 ? <p className="muted">以下问题收到相同答复，合并展示。</p> : null}
              <p className="channel-decision-answer">{first.answer || "未提供答复正文"}</p>
              {first.riskNote ? <p>风险说明：{first.riskNote}</p> : null}
              {group.receipts.map((receipt) => <details className="channel-decision-record" data-testid="channel-decision-record" key={receipt.id}>
                <summary>原始记录 · {receipt.askedBy || "未记录提问人"} · {receipt.id}</summary>
                <p>{receipt.question}</p>
                <p>答复人：{receipt.resolvedBy || "未记录"} · 处置：{receipt.resolution || "未记录"}</p>
                {!receipt.sourceRunId && !receipt.sourceMessageId ? <p className="muted">原记录未绑定具体回复；来源以问题和事件记录为准。</p> : null}
                <pre aria-label={`原始记录 ${receipt.id}`}>{JSON.stringify(receipt.original, null, 2)}</pre>
              </details>)}
            </details>;
          })}
        </section>;
      })}
    </div> : null}
  </details>;
}
