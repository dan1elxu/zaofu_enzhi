import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";
import { getChannelDetail } from "../api/client";
import type { ChannelDetail } from "../api/types";
import { LatestRequestGate } from "./latestRequestGate";

export function useChannelDiagnostics({ activeProjectId, selectedChannelId, selectedChannelIdRef, conversationSeq }: {
  activeProjectId: string;
  selectedChannelId: string;
  selectedChannelIdRef: MutableRefObject<string>;
  conversationSeq: number;
}) {
  const [channelDiagnosticsDetail, setChannelDiagnosticsDetail] = useState<ChannelDetail | null>(null);
  const [channelDiagnosticsLoading, setChannelDiagnosticsLoading] = useState(false);
  const [channelDiagnosticsError, setChannelDiagnosticsError] = useState<string | null>(null);
  const channelDiagnosticsRequestGateRef = useRef(new LatestRequestGate());
  const invalidateChannelDiagnostics = useCallback(() => {
    channelDiagnosticsRequestGateRef.current.invalidate();
    setChannelDiagnosticsDetail(null);
    setChannelDiagnosticsLoading(false);
    setChannelDiagnosticsError(null);
  }, []);
  useEffect(invalidateChannelDiagnostics, [activeProjectId, selectedChannelId, invalidateChannelDiagnostics]);

  const loadChannelDiagnostics = useCallback(async () => {
    const channelId = selectedChannelId || "ch-zaofu";
    if (channelDiagnosticsLoading) return;
    if (
      channelDiagnosticsDetail?.channel_id === channelId
      && Number(channelDiagnosticsDetail.seq || 0) >= conversationSeq
    ) return;
    const projectId = activeProjectId || undefined;
    const ticket = channelDiagnosticsRequestGateRef.current.issue();
    setChannelDiagnosticsLoading(true);
    setChannelDiagnosticsError(null);
    try {
      const detail = await getChannelDetail(channelId, projectId);
      if (!channelDiagnosticsRequestGateRef.current.isCurrent(ticket)) return;
      if (selectedChannelIdRef.current !== channelId) return;
      setChannelDiagnosticsDetail(detail);
    } catch (err) {
      if (!channelDiagnosticsRequestGateRef.current.isCurrent(ticket)) return;
      setChannelDiagnosticsError(err instanceof Error ? err.message : String(err));
    } finally {
      if (channelDiagnosticsRequestGateRef.current.isCurrent(ticket)) {
        setChannelDiagnosticsLoading(false);
      }
    }
  }, [activeProjectId, conversationSeq, channelDiagnosticsDetail, channelDiagnosticsLoading, selectedChannelId]);

  return { channelDiagnosticsDetail, channelDiagnosticsLoading, channelDiagnosticsError,
    loadChannelDiagnostics, invalidateChannelDiagnostics };
}
