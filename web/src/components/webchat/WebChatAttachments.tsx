import { FilePlus2, Loader2, Paperclip, X } from "lucide-react";
import { useRef, useState } from "react";
import { uploadWebChatArtifact } from "../../api/client";
import type { WebChatArtifact } from "../../api/types";

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Unable to read file"));
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve(value.includes(",") ? value.slice(value.indexOf(",") + 1) : value);
    };
    reader.readAsDataURL(file);
  });
}

export function WebChatAttachments({
  artifacts,
  conversationId,
  disabled,
  onChange,
  onError,
  projectId,
  threadId,
}: {
  artifacts: WebChatArtifact[];
  conversationId: string;
  disabled?: boolean;
  onChange: (items: WebChatArtifact[]) => void;
  onError: (message: string) => void;
  projectId: string;
  threadId: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [uploading, setUploading] = useState(false);

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    onError("");
    const next = [...artifacts];
    try {
      for (const file of Array.from(files)) {
        const result = await uploadWebChatArtifact(projectId, {
          logical_name: `attachments/${file.name}`,
          media_type: file.type || "application/octet-stream",
          data_base64: await fileBase64(file),
          lineage: { conversation_id: conversationId, thread_id: threadId },
        });
        if (!next.some((item) => item.version_id === result.artifact.version_id)) {
          next.push(result.artifact);
        }
      }
      onChange(next);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="webchat-attachments">
      <input
        accept=".txt,.md,.json,.pdf,.png,.jpg,.jpeg,.gif,.zip"
        aria-label="Attach files"
        hidden
        multiple
        ref={inputRef}
        type="file"
        onChange={(event) => void upload(event.target.files)}
      />
      <button
        aria-label="Attach files"
        className="webchat-composer-icon"
        disabled={disabled || uploading}
        title="Attach files"
        type="button"
        onClick={() => inputRef.current?.click()}
      >
        {uploading ? <Loader2 className="spin" size={16} /> : <Paperclip size={16} />}
      </button>
      {artifacts.map((item) => (
        <span className="webchat-attachment-chip" key={item.version_id}>
          <FilePlus2 size={13} />
          <span>{item.logical_name.split("/").pop()}</span>
          <button
            aria-label={`Remove ${item.logical_name}`}
            type="button"
            onClick={() => onChange(artifacts.filter((candidate) => candidate.version_id !== item.version_id))}
          >
            <X size={12} />
          </button>
        </span>
      ))}
    </div>
  );
}
