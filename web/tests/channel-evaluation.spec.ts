import { expect, test, type Page, type Route } from "@playwright/test";

const projectId = process.env.ZF_EVALUATION_E2E_PROJECT_ID ?? "";
const channelId = "ch-1";
const secondChannelId = "ch-2";
test.skip(!projectId, "requires an evaluation fixture project");

type Evaluation = {
  schema_version: string;
  channel_id: string;
  workflow_run_id: string;
  evaluation_origin?: "manual_regrade";
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

for (const partialScore of [false, true]) {
  test(`missing PRD evidence remains visible with partial score=${partialScore}`, async ({ page }) => {
    // Historical results remain visible under their original judgment;
    // the current output gate does not rewrite persisted scores or reasons.
    const payload = evaluationPayload("incomplete");
    const fidelity = payload.stages.find((stage) => stage.judge_type === "prd_fidelity")!;
    fidelity.status = "evidence_missing";
    fidelity.result = {
      missing_evidence: ["G-VERIFY-01：缺少可运行验证入口及真实身份、附件依赖接入证据。"],
      dimensions: partialScore ? [
        { dimension_id: "original_to_channel_prd", score: 4, status: "scored", rationale: "已完成需求内容对齐。" },
      ] : [],
    };
    await mockEvaluation(page, async (route) => route.fulfill({
      contentType: "application/json", body: JSON.stringify(payload),
    }));
    await openEvaluation(page);
    const card = page.getByTestId("channel-evaluation-page").locator(".channel-evaluation-card")
      .filter({ has: page.getByRole("heading", { name: "PRD 忠实度", exact: true }) });
    await expect(card).toBeVisible();
    await expect(card).toContainText("缺少证据");
    await expect(card.getByTestId("channel-evaluation-missing-evidence")).toContainText("G-VERIFY-01");
    await expect(card).toContainText("真实身份、附件依赖接入证据");
    await expect(card).toContainText(partialScore ? "4/4" : "本项尚未完成评分");
    await expect(card).not.toContainText("0/4");
  });
}

test("implementation follow-ups appear separately from scored PRD fidelity", async ({ page }) => {
  const payload = evaluationPayload();
  const fidelity = payload.stages.find((stage) => stage.judge_type === "prd_fidelity")!;
  fidelity.result = {
    dimensions: [{ dimension_id: "original_to_channel_prd", score: 4, status: "scored" }],
    missing_evidence: [],
    differences: ["后续事项：开发阶段补充验证入口、接入身份及附件服务。"],
  };
  await mockEvaluation(page, async (route) => route.fulfill({
    contentType: "application/json", body: JSON.stringify(payload),
  }));
  await openEvaluation(page);
  const evalPage = page.getByTestId("channel-evaluation-page");
  await expect(evalPage).toContainText("4/4");
  await expect(evalPage.getByTestId("channel-evaluation-missing-evidence")).toHaveCount(0);
  await expect(evalPage.getByTestId("channel-evaluation-differences")).toContainText("后续事项：开发阶段");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
});

test("a finished regrade automatically replaces the open report and status", async ({ page }) => {
  let latest = false;
  let captureStream!: (route: Route) => void;
  const stream = new Promise<Route>((resolve) => { captureStream = resolve; });
  await page.route("**/api/projects/*/stream?*", async (route) => {
    if (latest) await route.abort();
    else captureStream(route);
  });
  await page.route("**/api/projects/*/channels/ch-1/conversation?*", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    await route.fulfill({ response, json: { ...body, seq: latest ? 1_000_000 : body.seq } });
  });
  await mockEvaluation(page, async (route) => {
    const payload = evaluationPayload(latest ? "complete" : "incomplete", channelId, latest ? "NEW_REGRADE" : "OLD_REPORT");
    const fidelity = payload.stages.find((stage) => stage.judge_type === "prd_fidelity")!;
    fidelity.status = latest ? "scored" : "evidence_missing";
    if (latest) {
      payload.workflow_run_id = "regrade-new";
      payload.evaluation_origin = "manual_regrade";
    }
    await route.fulfill({ json: payload });
  });
  await openEvaluation(page);
  const evalPage = page.getByTestId("channel-evaluation-page");
  await expect(evalPage).toContainText("OLD_REPORT");
  await evalPage.locator(".channel-evaluation-report summary").click();
  latest = true;
  await (await stream).fulfill({
    contentType: "text/event-stream",
    body: `id: 1000000\ndata: ${JSON.stringify({
      type: "benchmark.evaluation.conclusion.completed", seq: 1_000_000,
      payload: { channel_id: channelId, evaluation_origin: "manual_regrade" },
    })}\n\n`,
  });
  await expect(evalPage).toContainText("最新重评");
  await expect(evalPage.locator(".channel-evaluation-report")).toContainText("NEW_REGRADE");
  await expect(evalPage).not.toContainText("OLD_REPORT");
  await expect(page.getByTestId("channel-evaluation-status").locator("summary")).not.toContainText("缺少证据");
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
