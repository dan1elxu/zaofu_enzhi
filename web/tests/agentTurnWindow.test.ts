import { windowAgentTurns } from "../src/components/agent-session/turnWindow.js";
import type { AgentSessionTurn } from "../src/components/agent-session/types.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

function turn(id: string, status: "completed" | "streaming" | "waiting_input"): AgentSessionTurn {
  return {
    id,
    threadId: "main",
    ts: "2026-09-30T00:00:00Z",
    cards: [],
    runs: [{ id: `run-${id}`, threadId: "main", status, parts: [] }],
  };
}

const turns = [
  ...Array.from({ length: 320 }, (_, index) => turn(`done-${index}`, "completed")),
  turn("active-question", "waiting_input"),
  turn("active-stream", "streaming"),
];
const result = windowAgentTurns(turns, 120);

assert(result.hiddenSettledCount === 200, "only settled turns are windowed");
assert(result.turns.length === 122, "120 settled plus both active turns remain");
assert(result.turns.some((item) => item.id === "active-question"), "waiting-input controls stay mounted");
assert(result.turns.some((item) => item.id === "active-stream"), "streaming turn stays mounted");
assert(!result.turns.some((item) => item.id === "done-0"), "oldest settled turn is hidden");

console.log("agentTurnWindow.test.ts OK");
