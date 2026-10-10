import { useRef } from "react";
import { AskUserQuestion, type AskUserQuestionItem, type AskUserQuestionAnswer } from "../common/AskUserQuestion";

export function ChannelQuestionCard({ question, requestId, busy, disabled, onDiscuss, onSubmit, sourceThreadId, defaultOpen = false }: {
  question: AskUserQuestionItem; requestId: string; busy: boolean; disabled: boolean;
  sourceThreadId?: string; defaultOpen?: boolean;
  onDiscuss: (question: AskUserQuestionItem) => void;
  onSubmit: (answers: AskUserQuestionAnswer[]) => void;
}) {
  const disclosure = useRef<HTMLDetailsElement>(null);
  const recommended = question.options?.find((option) => option.recommended);
  return <details ref={disclosure} open={defaultOpen || undefined} className="channel-question-card" data-question-id={question.id} data-question-thread={sourceThreadId}>
    <summary>
      <span className="channel-question-eyebrow">需要你确认</span>
      <strong>{question.question}</strong>
      {recommended ? <span className="channel-question-recommendation">建议：{recommended.label}</span> : null}
      <span className="channel-question-toggle">查看选项并回答</span>
    </summary>
    <AskUserQuestion questions={[question]} requestId={`${requestId}:${question.id}`} busy={busy} disabled={disabled}
      onClose={() => { if (disclosure.current) disclosure.current.open = false; }}
      onDiscuss={onDiscuss} onSubmit={onSubmit} submitLabel="Answer" variant="embedded" />
  </details>;
}
