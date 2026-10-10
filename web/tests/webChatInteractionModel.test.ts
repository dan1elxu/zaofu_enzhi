import type { WebChatInteraction } from "../src/api/types.js";
import {
  approvalDecisions,
  buildQuestionResponse,
  emptyInteractionDraft,
  interactionDraftStorageKey,
  interactionKey,
  loadInteractionDrafts,
  normalizeWebChatQuestions,
  saveInteractionDrafts,
} from "../src/components/webchat/webChatInteractionModel.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

const interaction: WebChatInteraction = {
  interaction_id: "ask-1",
  turn_id: "turn-1",
  kind: "ask_user",
  revision: 3,
  request_digest: "digest",
  request: {
    header: "Clarify delivery",
    questions: [{
      id: "route",
      question: "Which route?",
      options: [{ id: "direct", label: "Direct" }, { id: "workflow", label: "Workflow" }],
      allow_other: true,
    }, {
      id: "evidence",
      question: "Which evidence?",
      choices: ["Tests", "Browser"],
      multi_select: true,
      allow_other: false,
    }],
  },
  status: "pending",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

assert(interactionKey(interaction) === "ask-1:3", "identity includes revision");
const questions = normalizeWebChatQuestions(interaction);
assert(questions.length === 2, "normalizes every question");
assert(questions[0].allowOther, "preserves free-text option");
assert(questions[1].multiSelect, "preserves multi-select option");

const response = buildQuestionResponse({
  answers: {
    route: { optionIds: ["workflow"], text: "Use the PRD" },
    evidence: { optionIds: ["Tests", "Browser"], text: "" },
  },
}, questions);
const answers = response.answers as Array<Record<string, unknown>>;
assert(answers.length === 2, "response keeps question boundaries");
assert(answers[0].answer === "Workflow, Use the PRD", "response combines option and context");
assert(Array.isArray(answers[1].option_ids) && answers[1].option_ids.length === 2, "response preserves multi-select ids");

const values = new Map<string, string>();
const storage = {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => { values.set(key, value); },
};
const storageKey = interactionDraftStorageKey("project", "conversation", "thread");
const drafts = { [interactionKey(interaction)]: { answers: { route: { optionIds: ["direct"], text: "" } } } };
saveInteractionDrafts(storage, storageKey, drafts);
assert(loadInteractionDrafts(storage, storageKey)["ask-1:3"].answers.route.optionIds[0] === "direct", "draft survives reload");
values.set(storageKey, "not-json");
assert(Object.keys(loadInteractionDrafts(storage, storageKey)).length === 0, "corrupt draft fails closed");
assert(Object.keys(emptyInteractionDraft().answers).length === 0, "empty draft has no answers");

assert(
  approvalDecisions({
    ...interaction,
    kind: "tool_approval",
    request: { allowed_decisions: [{ id: "allow_once" }, { value: "deny" }] },
  }).join(",") === "allow_once,deny",
  "approval decisions accept structured provider choices",
);

console.log("webChatInteractionModel.test.ts OK");
