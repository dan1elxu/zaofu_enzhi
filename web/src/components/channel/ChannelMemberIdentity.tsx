import { channelMemberLabel } from "./channelFlow";

export function memberColor(id: string) {
  const tones = ["var(--brand)", "var(--ok)", "var(--warn)", "var(--info, var(--brand))"];
  return tones[Array.from(id).reduce((hash, char) => hash * 31 + char.charCodeAt(0) >>> 0, 0) % tones.length];
}

export function ChannelMemberIdentity({ member, moderator, responder, onDetails }: {
  member: Record<string, unknown>; moderator: boolean; responder: boolean; onDetails: () => void;
}) {
  const label = channelMemberLabel(member);
  return <button className="channel-message-identity" type="button" onClick={onDetails}
    aria-label={`查看 ${label} 的成员信息`}>
    <span className="channel-message-avatar" style={{ color: memberColor(String(member.member_id || label)) }} aria-hidden="true">{Array.from(label).slice(0, 1).join("")}</span>
    <strong>{label}</strong>
    {moderator ? <small>主持人</small> : null}
    {responder ? <small>默认回复</small> : null}
  </button>;
}
