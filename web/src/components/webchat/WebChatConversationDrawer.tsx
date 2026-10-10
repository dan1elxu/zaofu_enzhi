import { Download, GitFork, Pencil, RefreshCw, Search, Sparkles, X } from "lucide-react";
import { useEffect, useState } from "react";
import {
  compactWebChatThread,
  correctWebChatMessage,
  forkWebChatMessage,
  getWebChatMessages,
  searchWebChatMessages,
  webChatExportUrl,
} from "../../api/client";
import type { WebChatSearchResult } from "../../api/types";
import { useWebChatDrawer } from "./useWebChatDrawer";

export function WebChatConversationDrawer({
  conversationId,
  onClose,
  onFork,
  onRetry,
  projectId,
  threadId,
}: {
  conversationId: string;
  onClose: () => void;
  onFork: (threadId: string) => void;
  onRetry: (content: string, messageId: string) => void;
  projectId: string;
  threadId: string;
}) {
  const drawerRef = useWebChatDrawer(onClose);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<WebChatSearchResult[]>([]);
  const [editing, setEditing] = useState<WebChatSearchResult | null>(null);
  const [editText, setEditText] = useState("");
  const [summary, setSummary] = useState("");
  const [messageIds, setMessageIds] = useState<string[]>([]);
  const [messages, setMessages] = useState<Array<WebChatSearchResult & { content: string }>>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    void getWebChatMessages(projectId, conversationId, threadId).then((page) => {
      setMessages(page.items);
      setMessageIds(page.items.map((item) => item.message_id));
    }).catch(() => {
      setMessages([]);
      setMessageIds([]);
    });
  }, [conversationId, projectId, threadId]);

  async function search() {
    if (!query.trim()) return;
    setBusy("search");
    setError("");
    try {
      setResults((await searchWebChatMessages(projectId, query, conversationId)).items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function correct() {
    if (!editing || !editText.trim()) return;
    setBusy("correct");
    try {
      await correctWebChatMessage(projectId, editing.message_id, editText.trim());
      setEditing(null);
      setQuery(editText.trim());
      setResults((await searchWebChatMessages(projectId, editText.trim(), conversationId)).items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function fork(item: WebChatSearchResult) {
    setBusy("fork");
    try {
      const response = await forkWebChatMessage(projectId, item.message_id, `Fork · ${item.preview.slice(0, 30)}`);
      const thread = response.thread as Record<string, unknown> | undefined;
      if (thread?.thread_id) onFork(String(thread.thread_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function compact() {
    if (!summary.trim() || !messageIds.length) return;
    setBusy("compact");
    try {
      await compactWebChatThread(projectId, conversationId, threadId, summary.trim(), messageIds);
      setSummary("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  function retry(item: WebChatSearchResult) {
    const sourceId = item.role === "assistant" ? item.parent_message_id : item.message_id;
    const source = messages.find((message) => message.message_id === sourceId);
    if (!source || source.role !== "user") {
      setError("The immutable input for this reply is not available in the current thread.");
      return;
    }
    onRetry(source.content, item.message_id);
  }

  return (
    <aside aria-label="Conversation tools" aria-modal="true" className="webchat-drawer" ref={drawerRef} role="dialog" tabIndex={-1}>
      <header className="webchat-drawer-header">
        <span><Search size={16} /> Conversation</span>
        <button aria-label="Close conversation tools" className="agent-window-button ghost" type="button" onClick={onClose}><X size={16} /></button>
      </header>
      <div className="webchat-conversation-tools">
        <form className="webchat-search" onSubmit={(event) => { event.preventDefault(); void search(); }}>
          <Search size={15} />
          <input aria-label="Search conversation" placeholder="Search this conversation" value={query} onChange={(event) => setQuery(event.target.value)} />
          <button disabled={!query.trim() || Boolean(busy)} type="submit">Search</button>
        </form>
        <a className="webchat-export" href={webChatExportUrl(projectId, conversationId, threadId)} download>
          <Download size={14} /> Export Markdown
        </a>
      </div>
      <div className="webchat-compact-form">
        <Sparkles size={15} />
        <textarea aria-label="Compact summary" placeholder="Write the durable compact summary..." value={summary} onChange={(event) => setSummary(event.target.value)} />
        <button disabled={!summary.trim() || !messageIds.length || Boolean(busy)} type="button" onClick={() => void compact()}>Compact</button>
      </div>
      {error ? <p className="webchat-resource-error" role="alert">{error}</p> : null}
      <div className="webchat-search-results">
        {results.map((item) => (
          <article key={item.message_id}>
            <small>{item.role} · {item.created_at.slice(0, 16)}</small>
            <p>{item.preview}</p>
            <div>
              {item.role === "assistant" ? (
                <button title="Retry from the original immutable input" type="button" onClick={() => retry(item)}><RefreshCw size={14} /> Retry</button>
              ) : null}
              {item.role === "user" ? (
                <button title="Create a corrected message" type="button" onClick={() => { setEditing(item); setEditText(item.preview); }}><Pencil size={14} /> Correct</button>
              ) : null}
              <button title="Fork from this message" type="button" onClick={() => void fork(item)}><GitFork size={14} /> Fork</button>
            </div>
          </article>
        ))}
      </div>
      {editing ? (
        <div className="webchat-correction" role="dialog" aria-label="Correct message">
          <textarea value={editText} onChange={(event) => setEditText(event.target.value)} />
          <div>
            <button type="button" onClick={() => setEditing(null)}>Cancel</button>
            <button disabled={!editText.trim() || Boolean(busy)} type="button" onClick={() => void correct()}>Create correction</button>
          </div>
        </div>
      ) : null}
    </aside>
  );
}
