import { ChevronRight, ListChecks, X } from "lucide-react";
import { useEffect, useId, useRef } from "react";

export interface ChannelDecisionItem {
  id: string;
  priority: string;
  question: string;
  askedBy: string;
  sourceThreadId: string;
  sourceRunId: string;
  sourceAvailable: boolean;
}

const priorityRank: Record<string, number> = { p0: 0, p1: 1, p2: 2 };

function priorityLabel(value: string): string {
  const priority = value.trim().toLowerCase();
  return priority ? priority.toUpperCase() : "DECISION";
}

function groupLabel(value: string): string {
  return value || "Unknown agent";
}

export function ChannelDecisionQueue({
  items,
  mobileOpen,
  onCloseMobile,
  onSelect,
  onToggleMobile,
}: {
  items: ChannelDecisionItem[];
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onSelect: (item: ChannelDecisionItem) => void;
  onToggleMobile: () => void;
}) {
  const bodyId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!mobileOpen) return;
    const frame = requestAnimationFrame(() => body.current?.querySelector<HTMLButtonElement>("button")?.focus());
    return () => cancelAnimationFrame(frame);
  }, [mobileOpen]);
  function closeSheet() {
    onCloseMobile();
    requestAnimationFrame(() => trigger.current?.focus());
  }
  if (!items.length) return null;
  const sorted = [...items].sort((left, right) => (
    (priorityRank[left.priority.toLowerCase()] ?? 9) - (priorityRank[right.priority.toLowerCase()] ?? 9)
  ));
  const grouped = new Map<string, ChannelDecisionItem[]>();
  for (const item of sorted) {
    const key = `${item.priority}:${item.askedBy}`;
    grouped.set(key, [...(grouped.get(key) ?? []), item]);
  }

  return (
    <aside
      aria-label="Decision queue"
      className={`channel-decision-queue ${mobileOpen ? "is-mobile-open" : ""}`.trim()}
      data-testid="channel-decision-queue"
      onClick={(event) => { if (mobileOpen && event.target === event.currentTarget) closeSheet(); }}
    >
      <button
        ref={trigger}
        aria-controls={bodyId}
        aria-expanded={mobileOpen}
        className="channel-decision-queue-trigger"
        type="button"
        onClick={onToggleMobile}
      >
        <ListChecks aria-hidden="true" size={16} />
        <span>Decision queue</span>
        <strong>{items.length}</strong>
        <ChevronRight aria-hidden="true" className="channel-decision-queue-trigger-icon" size={15} />
      </button>
      <div ref={body} id={bodyId} className="channel-decision-queue-body"
        role={mobileOpen ? "dialog" : undefined} aria-modal={mobileOpen || undefined}
        aria-label={mobileOpen ? "Decision queue" : undefined}
        onKeyDown={(event) => {
          if (!mobileOpen) return;
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeSheet(); }
          if (event.key === "Tab") {
            const buttons = body.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
            const first = buttons?.[0], last = buttons?.[buttons.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
          }
        }}>
        <div className="channel-decision-queue-heading">
          <div>
            <strong>Needs your decision</strong>
            <span>{items.length} pending · highest priority first</span>
          </div>
          <button aria-label="Close decision queue" className="channel-tool-button channel-decision-queue-close" type="button" onClick={closeSheet}>
            <X size={15} />
          </button>
        </div>
        <div className="channel-decision-queue-groups">
          {[...grouped].map(([key, group]) => (
            <section key={key}>
              <h4>{groupLabel(group[0].askedBy)} <span>{group.length}</span></h4>
              {group.map((item) => (
                <button
                  className="channel-decision-queue-item"
                  data-question-id={item.id}
                  key={JSON.stringify([item.sourceThreadId, item.id])}
                  type="button"
                  onClick={() => onSelect(item)}
                >
                  <span className="channel-decision-queue-item-meta">{priorityLabel(item.priority)} · {groupLabel(item.askedBy)}</span>
                  <strong>{item.question}</strong>
                  <span className="channel-decision-queue-item-action">{item.sourceAvailable ? "Open source question" : "Review question · source unavailable"} <ChevronRight size={14} /></span>
                </button>
              ))}
            </section>
          ))}
        </div>
      </div>
    </aside>
  );
}
