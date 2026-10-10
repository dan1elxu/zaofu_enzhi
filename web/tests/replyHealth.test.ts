import { channelReplyHealth, replyFailurePresentation } from "../src/components/channel/replyHealth.js";
function assert(value: unknown, message: string): asserts value { if (!value) throw new Error(message); }
const failure = { request_id: "first", thread_id: "main", message_id: "m1", target_member_id: "arch", status: "failed", run_generation: 1, updated_at: "2026-09-23T10:00:00Z" };
const rows = [failure, { ...failure, request_id: "second", message_id: "m2", status: "completed", updated_at: "2026-09-23T11:00:00Z" },
  { ...failure, request_id: "third", thread_id: "other", status: "running" }];
const before = JSON.stringify(rows);
let health = channelReplyHealth(rows, "main");
assert(health.failed.length === 1 && health.active.length === 0, "later unrelated success cannot hide failure; another thread is not active here");
health = channelReplyHealth([...rows, { ...failure, run_generation: 2, status: "completed" }, { ...failure, updated_at: "2026-09-23T12:00:00Z" }], "main");
assert(!health.failed.length, "late old generation cannot revive recovered request");
health = channelReplyHealth([failure, { ...failure, request_id: "queued", status: "queued" }, { ...failure, request_id: "working", status: "started" }], "main");
assert(health.running.length === 1 && health.waiting.length === 1 && health.failed.length === 1, "waiting and failure are not running");
for (const category of ["cancelled", "superseded"]) {
  assert(channelReplyHealth([{ ...failure, failure_category: category }], "main").failed.length === 0, "intentional settlement is not Provider failure");
}
assert(replyFailurePresentation({ failure_category: "timeout" }).label === "回复超时", "typed timeout");
assert(replyFailurePresentation({ failure_category: "rate_limit" }).label === "Provider 限流", "typed rate limit");
assert(replyFailurePresentation({ retryable: false }).advice.includes("不会自动重试"), "nonretryable remedy");
assert(JSON.stringify(rows) === before, "read projection never mutates input");
console.log("replyHealth tests passed");
