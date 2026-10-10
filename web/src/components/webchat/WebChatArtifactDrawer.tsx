import { CheckCircle2, Copy, Download, Eye, GitCompare, Package, Send, X } from "lucide-react";
import { useMemo, useState } from "react";
import {
  adoptWebChatArtifact,
  compareWebChatArtifacts,
  getWebChatArtifact,
  getWebChatTaskAdoptionTarget,
  publishWebChatArtifact,
  webChatArtifactContentUrl,
} from "../../api/client";
import type { WebChatArtifact } from "../../api/types";
import { useWebChatDrawer } from "./useWebChatDrawer";

function shortDigest(value: string): string {
  return value.replace(/^sha256:/, "").slice(0, 10);
}

function sizeLabel(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function WebChatArtifactDrawer({
  artifacts,
  onClose,
  onChanged,
  projectId,
  taskId,
}: {
  artifacts: WebChatArtifact[];
  onClose: () => void;
  onChanged: () => Promise<void> | void;
  projectId: string;
  taskId?: string;
}) {
  const drawerRef = useWebChatDrawer(onClose);
  const [selectedId, setSelectedId] = useState(artifacts[0]?.version_id ?? "");
  const [preview, setPreview] = useState("");
  const [compareId, setCompareId] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const selected = artifacts.find((item) => item.version_id === selectedId) ?? artifacts[0];
  const siblings = useMemo(
    () => artifacts.filter((item) => item.artifact_id === selected?.artifact_id && item.version_id !== selected?.version_id),
    [artifacts, selected],
  );

  async function inspect(item: WebChatArtifact) {
    setSelectedId(item.version_id);
    setBusy("preview");
    setError("");
    try {
      const detail = await getWebChatArtifact(projectId, item.version_id);
      setPreview(detail.preview || `${item.media_type} · ${sizeLabel(item.size_bytes)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function publish(item: WebChatArtifact) {
    setBusy("publish");
    setError("");
    try {
      await publishWebChatArtifact(projectId, item.version_id, item.digest);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function adopt(item: WebChatArtifact) {
    setBusy("adopt");
    setError("");
    try {
      const target = taskId
        ? await getWebChatTaskAdoptionTarget(projectId, taskId)
        : { kind: "plan" };
      await adoptWebChatArtifact(projectId, item.version_id, item.digest, target);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function compare() {
    if (!selected || !compareId) return;
    setBusy("compare");
    setError("");
    try {
      setPreview((await compareWebChatArtifacts(projectId, compareId, selected.version_id)).diff || "No differences");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  return (
    <aside aria-label="Artifacts" aria-modal="true" className="webchat-drawer" ref={drawerRef} role="dialog" tabIndex={-1}>
      <header className="webchat-drawer-header">
        <span><Package size={16} /> Artifacts</span>
        <button aria-label="Close artifacts" className="agent-window-button ghost" type="button" onClick={onClose}>
          <X size={16} />
        </button>
      </header>
      <div className="webchat-drawer-body">
        <div className="webchat-resource-list">
          {artifacts.length ? artifacts.map((item) => (
            <button
              className={`webchat-artifact-row ${item.version_id === selected?.version_id ? "active" : ""}`}
              key={item.version_id}
              type="button"
              onClick={() => void inspect(item)}
            >
              <span className={`webchat-visibility ${item.visibility}`}>{item.visibility}</span>
              <strong>{item.logical_name}</strong>
              <small>{item.media_type} · {sizeLabel(item.size_bytes)} · {shortDigest(item.digest)}</small>
            </button>
          )) : <p className="muted">No artifacts in this conversation.</p>}
        </div>
        {selected ? (
          <section className="webchat-resource-detail">
            <div className="webchat-resource-title">
              <strong>{selected.logical_name}</strong>
              <span className={`webchat-visibility ${selected.visibility}`}>{selected.visibility}</span>
            </div>
            <dl className="webchat-resource-meta">
              <div><dt>Version</dt><dd className="mono">{selected.version_id.slice(-12)}</dd></div>
              <div><dt>Digest</dt><dd className="mono">{shortDigest(selected.digest)}</dd></div>
              <div><dt>Producer</dt><dd>{String(selected.producer.agent_id || "legacy")}</dd></div>
            </dl>
            <div className="webchat-resource-actions">
              <button type="button" onClick={() => void inspect(selected)}><Eye size={14} /> Preview</button>
              <a href={webChatArtifactContentUrl(projectId, selected.version_id)} download><Download size={14} /> Download</a>
              <button type="button" onClick={() => void navigator.clipboard.writeText(selected.storage_uri)}><Copy size={14} /> Copy ref</button>
              {selected.visibility === "private" ? (
                <button disabled={Boolean(busy)} type="button" onClick={() => void publish(selected)}><Send size={14} /> Publish</button>
              ) : null}
              {selected.visibility === "published" ? (
                <button disabled={Boolean(busy)} type="button" onClick={() => void adopt(selected)}><CheckCircle2 size={14} /> Adopt</button>
              ) : null}
            </div>
            {siblings.length ? (
              <div className="webchat-compare-row">
                <select aria-label="Compare artifact version" value={compareId} onChange={(event) => setCompareId(event.target.value)}>
                  <option value="">Compare with...</option>
                  {siblings.map((item) => <option key={item.version_id} value={item.version_id}>{shortDigest(item.digest)}</option>)}
                </select>
                <button disabled={!compareId || Boolean(busy)} type="button" onClick={() => void compare()}><GitCompare size={14} /></button>
              </div>
            ) : null}
            {error ? <p className="webchat-resource-error" role="alert">{error}</p> : null}
            <pre className="webchat-artifact-preview">{busy === "preview" ? "Loading..." : preview || "Select Preview to inspect this version."}</pre>
          </section>
        ) : null}
      </div>
    </aside>
  );
}
