import { expect, test } from "@playwright/test";

// Run against the isolated evaluation fixture described in the implementation
// notes. The fixture contains only EventWriter-produced Channel/eval events.
const projectId = process.env.ZF_EVALUATION_E2E_PROJECT_ID ?? "";
test.skip(!projectId, "requires an isolated evaluation fixture project");

for (const [status, label] of [
  ["queued", "排队中"],
  ["running", "评估中"],
  ["retryable_failure", "等待重试"],
  ["inconclusive", "无法确定"],
]) {
  test(`Channel shows evaluation ${status}`, async ({ page, request }) => {
    const channelId = `ch-eval-${status}`;
    const response = await request.get(
      `/api/projects/${encodeURIComponent(projectId)}/channels/${channelId}/conversation`,
    );
    expect(response.ok()).toBeTruthy();
    const projection = await response.json();
    expect(projection.evaluation.stages[0].status).toBe(status);

    await page.goto(`/?project=${encodeURIComponent(projectId)}&page=channels&channel=${channelId}`);
    // Eval owns the live stage progress; the conversation has no summary banner.
    await expect(page.getByTestId("channel-evaluation-status")).toHaveCount(0);
    await page.getByTestId("channel-evaluation-button").click();
    const evaluation = page.getByTestId("channel-evaluation-page");
    await expect(evaluation).toBeVisible();
    await expect(evaluation).toContainText(label);
    if (status === "inconclusive") {
      await expect(evaluation).toContainText("尝试 2/2");
      await expect(evaluation).toContainText("provider failed twice");
      await page.reload();
      await page.getByTestId("channel-evaluation-button").click();
      await expect(page.getByTestId("channel-evaluation-page")).toContainText(label);
    }
  });
}
