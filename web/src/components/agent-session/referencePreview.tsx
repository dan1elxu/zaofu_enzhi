import { createContext } from "react";
import type { PreviewItem } from "./previewRegistry";

export const ReferencePreviewContext = createContext<((item: PreviewItem) => void) | null>(null);
export function previewFileRef(item: PreviewItem): string {
  for (const key of ["path", "file", "ref", "artifact_ref", "content_ref", "raw_ref"]) {
    if (typeof item.ref[key] === "string" && item.ref[key]) return item.ref[key] as string;
  }
  return "";
}
