import {
  Activity,
  Package,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Wrench,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

export type WebChatDrawerKind = "conversation" | "artifacts" | "activity" | "profile" | "";

function permissionLabel(value: string): string {
  return value.trim().replaceAll("_", " ") || "default";
}

export function WebChatResourceMenu({
  active,
  activityCount,
  artifactCount,
  onChange,
  permissionProfile,
  toolCount,
}: {
  active: WebChatDrawerKind;
  activityCount: number;
  artifactCount: number;
  onChange: (kind: WebChatDrawerKind) => void;
  permissionProfile: string;
  toolCount: number;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return undefined;
    function closeOutside(event: PointerEvent) {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    }
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  function select(kind: Exclude<WebChatDrawerKind, "">) {
    onChange(active === kind ? "" : kind);
    setOpen(false);
  }

  function item(
    kind: Exclude<WebChatDrawerKind, "">,
    label: string,
    description: string,
    icon: ReactNode,
    count?: number,
  ) {
    return (
      <button
        aria-checked={active === kind}
        className={active === kind ? "active" : ""}
        role="menuitemcheckbox"
        type="button"
        onClick={() => select(kind)}
      >
        {icon}
        <span>
          <strong>{label}</strong>
          <small>{description}</small>
        </span>
        {count ? <em>{count}</em> : null}
      </button>
    );
  }

  return (
    <div className="webchat-resource-menu" ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="Tools and resources"
        className={`webchat-composer-icon ${active ? "active" : ""}`}
        ref={triggerRef}
        title="Tools and resources"
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <SlidersHorizontal size={17} />
      </button>
      {open ? (
        <div aria-label="Tools and resources" className="webchat-resource-popover" role="menu">
          <div className="webchat-resource-menu-title">Tools and resources</div>
          {item("conversation", "Conversation", "Search, export, fork, or correct", <Search size={17} />)}
          {item("artifacts", "Artifacts", "Inspect and adopt durable outputs", <Package size={17} />, artifactCount)}
          {item("activity", "Activity", "Follow child agents and controls", <Activity size={17} />, activityCount)}
          {item("profile", "Agent profile", "Provider and permission proposal", <Settings2 size={17} />)}
          <div className="webchat-resource-menu-status" aria-label="Agent runtime status">
            <span title="Pinned permission profile">
              <ShieldCheck size={14} />
              <span>Permission</span>
              <strong>{permissionLabel(permissionProfile)}</strong>
            </span>
            <span title="Available tool catalog">
              <Wrench size={14} />
              <span>{toolCount} tools available</span>
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}

