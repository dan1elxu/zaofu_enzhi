import { expect, test, type Page, type Route } from "@playwright/test";

const projectId = process.env.ZF_EVALUATION_E2E_PROJECT_ID ?? "";
const channelId = "ch-1";
const secondChannelId = "ch-2";
test.skip(!projectId, "requires an evaluation fixture project");

type Evaluation = {
  schema_version: string;
  channel_id: string;
  workflow_run_id: string;
  status: string;
  stages: Array<Record<string, unknown>>;
  report: { available: boolean; markdown?: string; truncated?: boolean };
};

function evaluationPayload(
  status = "completed",
  requestedChannelId = channelId,
  marker = "CURRENT_FREEZE",
): Evaluation {
  return {
    schema_version: "channel.evaluation.v1",
    channel_id: requestedChannelId,
    workflow_run_id: "run-eval-1",
    status,
    stages: [
      {
        judge_type: "task_type", label: "任务类型", status: "scored",
        result_available: true, result: { task_type: "bugfix", confidence: 0.97 },
      },
      {
        judge_type: "contribution", label: "Agent 贡献", status: "scored",
        result_available: true,
        result: { agents: [
          { agent_id: "a-must", agent_name: "必须 Agent", score: 4, necessity: "must", status: "scored" },
          { agent_id: "a-optional", agent_name: "可选 Agent", score: null, necessity: "optional", status: "evidence_missing" },
          { agent_id: "a-no", agent_name: "不需要 Agent", score: 1, necessity: "not_needed", status: "scored" },
          { agent_id: "a-unknown", agent_name: "待定 Agent", score: null, necessity: "undetermined", status: "evidence_missing" },
        ] },
      },
      {
        judge_type: "discussion", label: "讨论有效性", status: "scored",
        result_available: true,
        result: { dimensions: [
          { dimension_id: "information_exchange", score: 3, verdict: "有效推进", status: "scored" },
          { dimension_id: "clarity", score: null, verdict: "证据不足", status: "evidence_missing" },
        ] },
      },
      {
        judge_type: "prd_fidelity", label: "PRD 忠实度", status: "scored",
        result_available: true,
        result: { dimensions: [{ dimension_id: "original_to_channel_prd", score: 3, verdict: "范围一致", status: "scored" }] },
      },
      {
        judge_type: "conclusion", label: "汇总结论", status: "complete",
        result_available: true,
        result: { conclusion_zh: `需求与讨论基本一致（${marker}）。`, next_actions: ["补齐证据"] },
      },
    ],
    report: { available: true, markdown: `# 评测报告\n\n${marker}` },
  };
}

async function mockEvaluation(page: Page, handler: (route: Route) => Promise<void>, requestedChannelId = channelId) {
  await page.route(
    `**/api/projects/*/channels/${encodeURIComponent(requestedChannelId)}/evaluation**`,
    handler,
  );
}

async function openEvaluation(page: Page) {
  await page.goto(
    `/?project=${encodeURIComponent(projectId)}&page=channels&channel=${encodeURIComponent(channelId)}`,
    { waitUntil: "domcontentloaded" },
  );
  await expect(page.locator(".channel-page")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("channel-evaluation-tab").click();
}

test("Eval tab renders the three score areas and explicit Agent necessity labels", async ({ page }) => {
  await mockEvaluation(page, async (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify(evaluationPayload()),
  }));
  await openEvaluation(page);

  const evalPage = page.getByTestId("channel-evaluation-page");
  await expect(evalPage).toBeVisible();
  await expect(evalPage).toContainText("PRD 忠实度");
  await expect(evalPage).toContainText("讨论有效性");
  await expect(evalPage).toContainText("Agent 贡献");
  await expect(evalPage).toContainText("必须");
  await expect(evalPage).toContainText("可选");
  await expect(evalPage).toContainText("不需要");
  await expect(evalPage).toContainText("未确定");
  await expect(evalPage).toContainText("3/4");
  await expect(evalPage).toContainText("汇总结论");
  const report = evalPage.locator(".channel-evaluation-report");
  await expect(report).not.toHaveAttribute("open", "");
  await report.locator("summary").click();
  await expect(report).toContainText("CURRENT_FREEZE");
  // Null scores are represented as an em dash; an unavailable score must not
  // silently become a misleading numeric zero.
  await expect(evalPage.locator(".channel-evaluation-agent").filter({ hasText: "可选 Agent" })).toContainText("—");
  await expect(evalPage.locator(".channel-evaluation-dimensions").filter({ hasText: "表达清晰度" })).toContainText("—");
});

test("Eval error exposes retry and recovers the latest response", async ({ page }) => {
  let attempts = 0;
  await mockEvaluation(page, async (route) => {
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "temporary" }) });
      return;
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(evaluationPayload()) });
  });
  await openEvaluation(page);
  await expect(page.locator(".channel-evaluation-page")).toContainText("returned 503");
  await page.getByTestId("channel-evaluation-retry").click();
  await expect(page.getByTestId("channel-evaluation-page")).toContainText("需求与讨论基本一致");
  expect(attempts).toBeGreaterThanOrEqual(2);
});

test("Eval tab keeps narrow layouts within the viewport", async ({ page }) => {
  await mockEvaluation(page, async (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify(evaluationPayload()),
  }));
  await openEvaluation(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("channel-evaluation-page")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

test("a late older evaluation response cannot overwrite a newer same-channel load", async ({ page }) => {
  let requests = 0;
  let releaseOld!: () => void;
  const oldResponse = new Promise<void>((resolve) => { releaseOld = resolve; });
  let firstRequested!: () => void;
  const firstRequest = new Promise<void>((resolve) => { firstRequested = resolve; });
  await mockEvaluation(page, async (route) => {
    requests += 1;
    if (requests === 1) {
      firstRequested();
      await oldResponse;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(evaluationPayload("completed", channelId, "STALE_FREEZE")),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(evaluationPayload("completed", channelId, "CURRENT_FREEZE")),
    });
  });
  await openEvaluation(page);
  await firstRequest;
  await page.getByTestId("channel-chat-tab").click();
  await page.getByTestId("channel-evaluation-tab").click();
  const evalPage = page.getByTestId("channel-evaluation-page");
  await expect(evalPage).toContainText("CURRENT_FREEZE");
  const staleResponse = page.waitForResponse((response) => (
    response.url().includes(`/channels/${channelId}/evaluation`)
  ));
  releaseOld();
  await staleResponse;
  await expect(evalPage).not.toContainText("STALE_FREEZE");
  expect(requests).toBeGreaterThanOrEqual(2);
});

test("a late response from ch-1 cannot appear after switching to ch-2", async ({ page }) => {
  let releaseFirst!: () => void;
  const firstResponse = new Promise<void>((resolve) => { releaseFirst = resolve; });
  let firstRequested!: () => void;
  const firstRequest = new Promise<void>((resolve) => { firstRequested = resolve; });
  await mockEvaluation(page, async (route) => {
    firstRequested();
    await firstResponse;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(evaluationPayload("completed", channelId, "STALE_CH1")),
    });
  });
  await mockEvaluation(page, async (route) => route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(evaluationPayload("completed", secondChannelId, "CURRENT_CH2")),
  }), secondChannelId);
  await openEvaluation(page);
  await firstRequest;
  await page.getByTitle("Switch channel").click();
  await page.getByPlaceholder("Search channels").fill(secondChannelId);
  await page.locator(".channel-switch-row").click();
  const evalPage = page.getByTestId("channel-evaluation-page");
  await expect(evalPage).toContainText("CURRENT_CH2");
  const staleResponse = page.waitForResponse((response) => (
    response.url().includes(`/channels/${channelId}/evaluation`)
  ));
  releaseFirst();
  await staleResponse;
  await expect(evalPage).not.toContainText("STALE_CH1");
});
