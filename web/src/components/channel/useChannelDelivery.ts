import { useEffect, useState } from "react";
import { channelDocumentKey, getChannelDelivery, type ChannelDeliveryEvidence, type ChannelPrdSelection } from "../../api/channelDelivery";

export function useChannelDelivery(selection: ChannelPrdSelection, refreshVersion: string) {
  const key = channelDocumentKey(selection);
  const [state, setState] = useState<{ key: string; data?: ChannelDeliveryEvidence; error?: string }>({ key: "" });
  useEffect(() => {
    const selected: ChannelPrdSelection = JSON.parse(key);
    if (!selected.digest || !selected.revision) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    const controller = new AbortController();
    async function refresh() {
      try {
        const data = await getChannelDelivery(selected, controller.signal);
        if (!disposed) setState({ key, data });
        failures = 0;
        const active = ["planning", "requested"].includes(data.planning.status || "")
          || data.runs.some((run) => ["running", "queued", "requested"].includes(run.status));
        if (!disposed && active) timer = setTimeout(refresh, 10_000);
      } catch (error) {
        if (!disposed) setState({ key, error: String(error) });
        failures += 1;
        if (!disposed && failures < 3) timer = setTimeout(refresh, 10_000);
      }
    }
    timer = setTimeout(() => void refresh(), 250);
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
    // Primitive identity: switching project/thread/version drops old evidence immediately.
  }, [key, refreshVersion]);
  return state.key === key ? state : { key };
}
