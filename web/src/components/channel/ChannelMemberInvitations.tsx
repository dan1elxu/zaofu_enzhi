import { useState } from "react";

export function ChannelMemberInvitations({ proposals, threadId, enabled, onDecide }: {
  proposals: Array<Record<string, unknown>>;
  threadId: string;
  enabled: boolean;
  onDecide: (proposal: Record<string, unknown>, decision: "approved" | "rejected") => Promise<void>;
}) {
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const rows = proposals.filter((row) => String(row.thread_id || "main") === threadId);
  if (!rows.length) return null;
  async function decide(row: Record<string, unknown>, decision: "approved" | "rejected") {
    setBusy(String(row.proposal_id));
    setError("");
    try { await onDecide(row, decision); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "邀请操作失败，请重试。"); }
    finally { setBusy(""); }
  }
  return <section data-testid="channel-member-invitations" aria-label="成员邀请">
    {rows.map((row) => <details className="channel-question-card" key={String(row.proposal_id)} open={row.status === "pending"}>
      <summary>{row.status === "pending" ? "主席建议补充成员" : row.status === "approved" ? "已批准并纳入讨论" : "已拒绝邀请"} · {String(row.member_id)}</summary>
      <p>{String(row.reason || "")}</p>
      <p>{String(row.channel_role)} · {String(row.profile_id)} · {row.required_reviewer ? "参与最终复审" : "提供专项意见"}</p>
      <small>{Array.isArray(row.source_refs) ? row.source_refs.join(" · ") : ""}</small>
      {row.status === "pending" ? <div className="channel-control-actions">
        <button type="button" disabled={!enabled || Boolean(busy)} onClick={() => void decide(row, "approved")}>批准并参评</button>
        <button type="button" disabled={!enabled || Boolean(busy)} onClick={() => void decide(row, "rejected")}>拒绝邀请</button>
      </div> : null}
    </details>)}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
