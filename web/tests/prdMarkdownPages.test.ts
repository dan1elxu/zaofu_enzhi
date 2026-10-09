import { prdMarkdownPages } from "../src/components/channel/prdMarkdownPages.js";

function assert(condition: unknown, message = "assertion failed"): void {
  if (!condition) throw new Error(message);
}

const sections = Array.from({ length: 8 }, (_, index) =>
  `## Section ${index}\r\n\r\n${"Readable text. ".repeat(600)}\r\n\r\n| Name | Value |\r\n|---|---|\r\n| ${index} | Required |\r\n\r\n`);
const markdown = "# Complete PRD\r\n\r\n" + sections.join("");
const pages = prdMarkdownPages(markdown);
assert(pages.length > 1);
assert(pages.map((page) => page.content).join("") === markdown, "pagination preserves every source character, including CRLF");
assert(pages.every((page) => page.content.length <= 50_000 && !page.plain));
for (const section of sections) assert(pages.some((page) => page.content.includes(section)), "sections and tables stay intact");

for (const fence of ["````", "~~~~"]) {
  const code = `${fence}md\n# Example heading\n${fence.slice(0, 3)}\n## Still inside code\n${fence}\n\n`;
  const source = `# Start\n\n${"a ".repeat(24_500)}\n\n## Code example\n\n${code}${"b ".repeat(1_000)}\n\n## End\n`;
  const split = prdMarkdownPages(source);
  assert(split.map((page) => page.content).join("") === source);
  assert(split.some((page) => page.content.includes(code)), "headings inside fenced code never become page boundaries");
}

const oversized = "# Oversized section\n\n" + "Content with spaces.\n".repeat(12_000) + "FINAL-LINE";
const fallback = prdMarkdownPages(oversized);
assert(fallback.every((page) => page.plain && page.content.length <= 50_000));
assert(fallback.map((page) => page.content).join("") === oversized, "oversized sections are fully accessible through source pages");
assert(prdMarkdownPages("data:image/png;base64," + "A".repeat(6_000))[0].plain, "unbroken tokens retain the renderer guard");
assert(prdMarkdownPages("").length === 1 && prdMarkdownPages("")[0].content === "");
console.log("prdMarkdownPages.test.ts OK");
