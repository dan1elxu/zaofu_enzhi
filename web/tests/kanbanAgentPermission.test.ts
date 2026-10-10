import { resolveKanbanTurnPermission } from "../src/components/orchestrator/kanbanAgentPermission.js";
import type { WebChatRuntime } from "../src/api/types.js";

function assert(condition: unknown, message: string): void {
  if (!condition) throw new Error(message);
}

function runtime(permissionProfile?: string): WebChatRuntime {
  return {
    schema_version: "webchat-runtime.v1",
    project_id: "project-1",
    conversation_id: "kanban:project-1",
    thread_id: "thread-1",
    capabilities: {
      actions: [],
      controls: [],
      native_steer: false,
      steer_fallback: "queue",
      completion_authority: true,
    },
    turns: [],
    interactions: [],
    queue: [],
    threads: [],
    artifacts: [],
    tool_catalog: {
      schema_version: "agent-tool-catalog.v1",
      digest: "sha256:test",
      surface: "kanban_agent",
      credentials_exposed: false,
      items: [],
    },
    agent_profile: permissionProfile ? {
      profile_id: "kanban-agent",
      revision: 1,
      profile_digest: "sha256:profile",
      profile: { permission_profile: permissionProfile },
    } : undefined,
  };
}

let loads = 0;
let resolved = await resolveKanbanTurnPermission({
  projectId: "project-1",
  runtime: runtime("read_only"),
  fallback: "dangerous_full",
  loadRuntime: async () => {
    loads += 1;
    return runtime("project_writer");
  },
});
assert(resolved.permissionProfile === "read_only", "loaded pinned profile wins");
assert(loads === 0, "loaded runtime does not trigger another request");

resolved = await resolveKanbanTurnPermission({
  projectId: "project-1",
  runtime: null,
  fallback: "dangerous_full",
  loadRuntime: async () => {
    loads += 1;
    return runtime("read_only");
  },
});
assert(resolved.permissionProfile === "read_only", "fresh durable profile wins over fallback");
assert(resolved.runtime?.agent_profile?.revision === 1, "fresh runtime is returned to the caller");

resolved = await resolveKanbanTurnPermission({
  override: "dangerous_full",
  projectId: "project-1",
  runtime: runtime("read_only"),
  fallback: "read_only",
  loadRuntime: async () => {
    throw new Error("explicit override must not fetch runtime");
  },
});
assert(resolved.permissionProfile === "dangerous_full", "explicit escalation stays explicit");

resolved = await resolveKanbanTurnPermission({
  projectId: "project-1",
  runtime: null,
  fallback: "project_writer",
  loadRuntime: async () => {
    throw new Error("runtime unavailable");
  },
});
assert(resolved.permissionProfile === "project_writer", "legacy fallback remains available");

resolved = await resolveKanbanTurnPermission({
  projectId: "project-2",
  runtime: runtime("read_only"),
  fallback: "dangerous_full",
  loadRuntime: async () => ({
    ...runtime("project_writer"),
    project_id: "project-2",
  }),
});
assert(resolved.permissionProfile === "project_writer", "stale project profile is never reused");

console.log("kanbanAgentPermission.test.ts OK");
