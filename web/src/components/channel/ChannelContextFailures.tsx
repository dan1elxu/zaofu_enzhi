import type { ChannelDiscussionAttention } from "../../api/types";

export function ChannelContextFailures({ rows }: { rows: NonNullable<ChannelDiscussionAttention["context_rejections"]> }) {
  if (!rows.length) return null;
  return <section data-testid="channel-context-failures" role="alert" className="channel-control-panel">
    <strong>来源上下文未通过检查</strong>
    <p>这些评审尚未派发。修正来源文件或项目上下文预算后，重新开始本轮讨论。</p>
    {rows.map((row) => <details key={row.context_pack_id}>
      <summary>{row.target_member_id} · 查看原因</summary><p>{row.reason}</p>
    </details>)}
  </section>;
}
