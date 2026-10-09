import { isPathologicalText, MAX_MARKDOWN_TEXT_LENGTH } from "../agent-session/markdownGuard.js";

export interface PrdMarkdownPage { content: string; plain: boolean }

/** Keep sections (including their tables and fenced code) intact when paging a PRD. */
export function prdMarkdownPages(markdown: string): PrdMarkdownPage[] {
  if (!markdown) return [{ content: "", plain: false }];
  const boundaries = [0];
  let offset = 0;
  let fence = "";
  for (const line of markdown.split(/(?<=\n)/)) {
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})(.*)/);
    if (fence) {
      if (marker && marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = "";
    } else if (marker) {
      fence = marker[1];
    } else if (offset && /^ {0,3}#{1,6}(?:[ \t]|\r?\n|$)/.test(line)) {
      boundaries.push(offset);
    }
    offset += line.length;
  }
  boundaries.push(markdown.length);
  const pages: PrdMarkdownPage[] = [];
  let pending = "";
  const flush = () => {
    if (pending) pages.push({ content: pending, plain: isPathologicalText(pending) });
    pending = "";
  };
  for (let index = 1; index < boundaries.length; index += 1) {
    const section = markdown.slice(boundaries[index - 1], boundaries[index]);
    if (pending.length + section.length > MAX_MARKDOWN_TEXT_LENGTH) flush();
    if (section.length > MAX_MARKDOWN_TEXT_LENGTH) {
      // An indivisible oversized section stays readable without feeding partial
      // tables/code blocks into the parser or dropping any source characters.
      for (let start = 0; start < section.length; start += MAX_MARKDOWN_TEXT_LENGTH) {
        pages.push({ content: section.slice(start, start + MAX_MARKDOWN_TEXT_LENGTH), plain: true });
      }
    } else {
      pending += section;
    }
  }
  flush();
  return pages;
}
