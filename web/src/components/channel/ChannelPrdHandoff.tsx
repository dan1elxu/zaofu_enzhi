import { FileText } from "lucide-react";
import { flowText, type ChannelFlow } from "./channelFlow";

export function ChannelPrdHandoff({ flow, onDetails }: { flow: ChannelFlow; onDetails: () => void }) {
  if (!flow.hasArtifact) return null;
  return <section className="channel-prd-handoff" data-testid="channel-prd-handoff">
    <div className="channel-prd-handoff-head">
      <div><strong>PRD · v{flow.revision} · {flow.statuses.prd}</strong>
        <span>{flow.ready ? "Ready for delivery planning" : "Review the current draft"}</span></div>
      <button className="icon-button" type="button" onClick={onDetails}>
        <FileText size={15} />Preview
      </button>
    </div>
    {flowText(flow.latest?.summary) ? <p className="channel-flow-body">{flowText(flow.latest?.summary)}</p> : null}
  </section>;
}
