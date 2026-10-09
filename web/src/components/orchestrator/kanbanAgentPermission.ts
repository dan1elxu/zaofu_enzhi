import type { WebChatRuntime } from "../../api/types";

type RuntimeLoader = () => Promise<WebChatRuntime>;

export interface ResolvedKanbanPermission {
  permissionProfile: string;
  runtime?: WebChatRuntime;
}

function pinnedPermissionProfile(runtime: WebChatRuntime | null): string {
  const value = runtime?.agent_profile?.profile?.permission_profile;
  return typeof value === "string" ? value.trim() : "";
}

export async function resolveKanbanTurnPermission({
  override,
  projectId,
  runtime,
  fallback,
  loadRuntime,
}: {
  override?: string;
  projectId: string;
  runtime: WebChatRuntime | null;
  fallback: string;
  loadRuntime: RuntimeLoader;
}): Promise<ResolvedKanbanPermission> {
  const explicit = override?.trim();
  if (explicit) return { permissionProfile: explicit };

  const pinned = runtime?.project_id === projectId
    ? pinnedPermissionProfile(runtime)
    : "";
  if (pinned) return { permissionProfile: pinned };

  try {
    const freshRuntime = await loadRuntime();
    return {
      permissionProfile: pinnedPermissionProfile(freshRuntime) || fallback,
      runtime: freshRuntime,
    };
  } catch {
    return { permissionProfile: fallback };
  }
}
