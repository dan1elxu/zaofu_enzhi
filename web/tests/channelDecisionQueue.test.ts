import type { ChannelDetail } from "../src/api/types";
import { openOwnerQuestionnaire } from "../src/components/channel/channelQuestionnaire.js";

function equal(actual: unknown, expected: unknown, message: string) {
  if (actual !== expected) throw new Error(`${message}: ${actual} !== ${expected}`);
}

const sourceThread = "kanban:project";
const detail = {
  open_questions: [
    { question_id: "same-id", thread_id: sourceThread, status: "open", asked_by: "security" },
    { question_id: "same-id", thread_id: "main", status: "resolved" },
  ],
  owner_questionnaires: {
    [sourceThread]: [{ question_id: "same-id", question: "Original decision" }],
    main: [{ question_id: "same-id", question: "Resolved decision" }],
  },
} as unknown as ChannelDetail;
equal(openOwnerQuestionnaire(detail, "main").length, 0, "main must not display another thread decision");
const questions = openOwnerQuestionnaire(detail, sourceThread);
equal(questions.length, 1, "same ID in another thread must not hide the open source question");
equal(questions[0].thread_id, sourceThread, "retain authoritative source thread for answering");
equal(questions[0].asked_by, "security", "retain ledger provenance when questionnaire omits it");
