import { expect, test } from "@playwright/test";

const enabled = process.env.ZF_AUTO_RETRY_E2E === "1";
test.skip(!enabled, "requires isolated auto-retry fixture API");

test("auto failure survives reload and retry queues the failed step", async ({ page, request }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => { errors.push(error.message); console.error(error.message); });
  await page.addInitScript(() => localStorage.setItem("zf.webActionToken", "auto-retry-test"));
  const snapshot = await (await request.get("/api/snapshot")).json();
  const projectId = snapshot.project.project_id;
  await page.goto(`/?project=${encodeURIComponent(projectId)}&page=channels&channel=ch-auto`);
  const status = page.getByTestId("channel-auto-status");
  await expect(status).toContainText("尝试 3/3");
  await expect(status).toContainText("Selected model is at capacity");
  await page.reload();
  await expect(status).toContainText("失败，需要重试");
  await expect(status.getByRole("button", { name: "重试", exact: true })).toBeEnabled();
  const accepted = page.waitForResponse((response) => response.url().includes("/actions/channel-auto-retry"));
  await status.getByRole("button", { name: "重试", exact: true }).click();
  expect((await accepted).status()).toBe(202);
  await expect(status).toContainText("等待执行");
  await expect(status).toContainText("尝试 0/3");
  await expect(status.getByRole("button", { name: "重试", exact: true })).toHaveCount(0);
  await page.reload();
  await expect(status).toContainText("等待执行");
  expect(errors).toEqual([]);
});
