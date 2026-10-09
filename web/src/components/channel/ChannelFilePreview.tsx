import { useEffect, useState } from "react";
import { Copy, Download, X } from "lucide-react";
import { cachedGetJson } from "../../api/queryClient";
import { MarkdownText } from "../agent-session/MarkdownText";
import { isPathologicalText } from "../agent-session/markdownGuard";
import { previewFileRef } from "../agent-session/referencePreview";
import type { PreviewItem } from "../agent-session/previewRegistry";

type Document = { name: string; ref: string; content: string; format: string; sha256: string; byte_count: number };
export function ChannelFilePreview({ item, projectId, channelId, threadId, onClose }: {
  item: PreviewItem; projectId: string; channelId: string; threadId: string; onClose: () => void;
}) {
  const [document, setDocument] = useState<Document>();
  const [error, setError] = useState("");
  const [source, setSource] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(0);
  const [copyStatus, setCopyStatus] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ ref: previewFileRef(item), thread_id: threadId });
    setError("");
    setDocument(undefined);
    void cachedGetJson<Document>(`/api/projects/${encodeURIComponent(projectId || "default")}/channels/${encodeURIComponent(channelId)}/file-preview?${params}`,
      { bypassCache: true, signal: controller.signal }).then((value) => {
      if (!controller.signal.aborted) setDocument(value);
    }).catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [item, projectId, channelId, threadId, attempt]);
  const large = Boolean(document && isPathologicalText(document.content));
  const pages = Math.ceil((document?.content.length || 0) / 50_000);
  return <section className="channel-document" data-testid="channel-file-preview" aria-label="File preview">
    <header className="channel-document-toolbar">
      <strong title={item.name}>{document?.name || item.name}</strong>
      <div>{document ? <>
        {document.format === "markdown" && !large ? <button className="icon-button" aria-pressed={source} type="button" onClick={() => setSource(!source)}>{source ? "Preview" : "Source"}</button> : null}
        <button className="icon-button" aria-label="Copy file" type="button" onClick={() => {
          void navigator.clipboard.writeText(document.content).then(() => setCopyStatus("Copied")).catch(() => setCopyStatus("Copy unavailable. Use Download."));
        }}><Copy size={14} /></button>
        <button className="icon-button" aria-label="Download file" type="button" onClick={() => {
          const href = URL.createObjectURL(new Blob([document.content], { type: "text/plain;charset=utf-8" }));
          const link = window.document.createElement("a"); link.href = href; link.download = document.name; link.click();
          setTimeout(() => URL.revokeObjectURL(href), 1000);
        }}><Download size={14} /></button>
      </> : null}<button className="icon-button" aria-label="Close preview" type="button" onClick={onClose}><X size={15} /></button></div>
    </header>
    <div className="channel-document-body">
      {error ? <div role="alert"><p>无法读取完整文件，未使用摘要替代正文。</p><small>{error}</small><button className="icon-button" type="button" onClick={() => setAttempt(attempt + 1)}>Retry</button></div>
        : !document ? <p role="status">Loading full document…</p> : null}
      {document ? <>
        {large ? <p>Large document · Source {page + 1}/{pages}. Copy / Download include the full file.</p> : null}
        {document.format === "markdown" && !source && !large ? <MarkdownText content={document.content} />
          : <pre data-testid="channel-file-source">{large ? document.content.slice(page * 50_000, (page + 1) * 50_000) : document.content}</pre>}
        {large && pages > 1 ? <div className="channel-document-pagination"><button type="button" className="icon-button" disabled={!page} onClick={() => setPage(page - 1)}>Previous</button><button type="button" className="icon-button" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next</button></div> : null}
        <details className="channel-document-evidence"><summary>File information</summary><code>{document.ref}</code><code>sha256:{document.sha256}</code><span>{document.byte_count} bytes · current file contents</span></details>
      </> : null}
    </div>
    <footer className="channel-document-footer"><p role="status">{copyStatus || "只读文件预览 · 文件不是已确认的 canonical PRD，不提供 Process。"}</p></footer>
  </section>;
}
