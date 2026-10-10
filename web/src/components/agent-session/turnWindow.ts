import type { AgentSessionTurn } from "./types";

export function isSettledTurn(turn: AgentSessionTurn): boolean {
  const runActive = turn.runs.some((run) => (
    run.status === "streaming"
    || run.status === "submitted"
    || run.status === "queued"
    || run.status === "waiting_input"
  ));
  const cardActive = turn.cards.some((card) => (
    card.status !== "completed" && card.status !== "stale" && card.status !== "cancelled"
  ));
  return !runActive && !cardActive;
}

export function windowAgentTurns(
  turns: AgentSessionTurn[],
  settledLimit: number,
): { hiddenSettledCount: number; turns: AgentSessionTurn[] } {
  const settled = turns.filter(isSettledTurn);
  const visibleSettledIds = new Set(
    settled.slice(-Math.max(0, settledLimit)).map((turn) => turn.id),
  );
  return {
    hiddenSettledCount: Math.max(0, settled.length - visibleSettledIds.size),
    turns: turns.filter((turn) => !isSettledTurn(turn) || visibleSettledIds.has(turn.id)),
  };
}
