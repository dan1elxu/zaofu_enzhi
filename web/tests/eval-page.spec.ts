import { expect, test, type Page, type Route } from "@playwright/test";

const projectId = process.env.ZF_EVAL_E2E_PROJECT_ID ?? "";
test.skip(!projectId, "requires an eval fixture project");

const DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const DIGEST_2 = "ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff00";
const RUN_ID = `${DIGEST}-trial-1`;
const RUN_ID_2 = `${DIGEST}-trial-2`;
const RUN_ID_3 = `${DIGEST_2}-trial-1`;

type LaneStatus = { status: string; error: string };

function lane(status: string, error = ""): LaneStatus {
  return { status, error };
}

function listPayload(runStatus = "completed") {
  return {
    schema_version: "eval.runs.v1",
    groups: [
      {
        prompt_digest: DIGEST,
        prompt_version: "eval-prompt.v1",
        trials: [
          {
            run_id: RUN_ID,
            trial: 1,
            status: runStatus,
            started_at: "2026-10-13T08:00:00Z",
            finished_at: "2026-10-13T08:40:00Z",
            lanes: {
              terminal: lane("prd_frozen"),
              kanban: lane("prd_frozen"),
              channel: lane("failed", "timeout"),
            },
            composite_scores: { terminal: 3.5, kanban: 2.8, channel: null },
            recommendation: ["A", "B"],
          },
          {
            run_id: RUN_ID_2,
            trial: 2,
            status: "running",
            started_at: "2026-10-13T09:00:00Z",
            finished_at: null,
            lanes: {
              terminal: lane("prd_frozen"),
              kanban: lane("running"),
              channel: lane("pending"),
            },
            composite_scores: null,
            recommendation: null,
            artifacts_source: RUN_ID,
          },
        ],
      },
      {
        prompt_digest: DIGEST_2,
        prompt_version: "eval-prompt.v1",
        trials: [
          {
            run_id: RUN_ID_3,
            trial: 1,
            status: "completed",
            started_at: "2026-10-12T07:00:00Z",
            finished_at: "2026-10-12T07:30:00Z",
            lanes: {
              terminal: lane("prd_frozen"),
              kanban: lane("prd_frozen"),
              channel: lane("prd_frozen"),
            },
            composite_scores: { terminal: 2.5, kanban: 3.0, channel: 3.5 },
            recommendation: ["C", "B", "A"],
          },
        ],
      },
    ],
  };
}

function detailPayload() {
  return {
    schema_version: "eval.run.v1",
    run_id: RUN_ID,
    prompt_digest: DIGEST,
    prompt_version: "eval-prompt.v1",
    prompt_text: "PROMPT_MARKER：做一个支持实时同步的三人协作待办应用。",
    trial: 1,
    status: "completed",
    lanes: {
      terminal: {
        status: "prd_frozen",
        error: "",
        started_at: "2026-10-13T08:00:00Z",
        finished_at: "2026-10-13T08:05:00Z",
        prd_markdown: "# Terminal PRD\n\nTERMINAL_PRD_BODY",
      },
      kanban: {
        status: "prd_frozen",
        error: "",
        started_at: "2026-10-13T08:00:00Z",
        finished_at: "2026-10-13T08:12:00Z",
        prd_markdown: "# Kanban PRD\n\nKANBAN_PRD_BODY",
      },
      channel: {
        status: "failed",
        error: "timeout",
        started_at: "2026-10-13T08:00:00Z",
        finished_at: "2026-10-13T08:30:00Z",
        prd_markdown: null,
      },
    },
    evaluation: {
      task_type: { status: "scored", task_type: "greenfield", confidence: 0.92 },
      lane_scores: {
        terminal: {
          status: "scored",
          dimensions: { coverage: 4, fidelity: 3, actionability: 3, structure: 4 },
          composite: 3.5,
          checklist: { total: 12, pass: 9, partial: 2, missing: 1 },
          concerns: [
            { dimension_id: "fidelity", text: "CONCERN_MARKER 范围扩写未登记" },
            { dimension_id: "actionability", text: "CONCERN_A2 验收命令缺超时" },
          ],
        },
        kanban: {
          status: "scored",
          dimensions: { coverage: 3, fidelity: 3, actionability: 2, structure: 3 },
          composite: 2.8,
          checklist: { total: 12, pass: 7, partial: 3, missing: 2 },
          concerns: [],
        },
        channel: null,
      },
      cross_compare: {
        status: "scored",
        recommendation: ["A", "B"],
        key_gaps: [
          { candidate: "B", gap: "验收标准缺失 KEY_GAP_MARKER" },
          { candidate: "B", gap: "缺少错误处理章节 KEY_GAP_B2" },
          { candidate: "A", gap: "性能预算未量化 KEY_GAP_A1" },
        ],
        dimension_rankings: [
          {
            dimension_id: "coverage",
            ranking: ["A", "B"],
            confidence: 4,
            rationale: "RATIONALE_MARKER Terminal 对需求清单覆盖更完整。",
          },
          { dimension_id: "fidelity", ranking: ["B", "A"], confidence: 2 },
        ],
        conflicts: [
          {
            dimension_id: "fidelity",
            blind_favorite: "A",
            pairwise_favorite: "B",
            resolution: "CONFLICT_MARKER 以逐段归因仲裁为准",
          },
        ],
      },
    },
    label_map: { A: "terminal", B: "kanban" },
    report_markdown: "# 评测报告\n\nREPORT_MARKER 推荐 Terminal。",
  };
}

async function mockEvalApi(
  page: Page,
  handlers: {
    list?: (route: Route) => Promise<void>;
    detail?: (route: Route) => Promise<void>;
  },
) {
  await page.route("**/api/**/eval/runs**", async (route) => {
    const url = route.request().url();
    if (/\/eval\/runs\/[^/?]+/.test(url)) {
      await (handlers.detail ?? ((inner) => inner.fulfill({
        contentType: "application/json",
        body: JSON.stringify(detailPayload()),
      })))(route);
      return;
    }
    await (handlers.list ?? ((inner) => inner.fulfill({
      contentType: "application/json",
      body: JSON.stringify(listPayload()),
    })))(route);
  });
}

async function openEvalPage(page: Page, runId = "") {
  const suffix = runId ? `&run_id=${encodeURIComponent(runId)}` : "";
  await page.goto(
    `/?project=${encodeURIComponent(projectId)}&page=eval${suffix}`,
    { waitUntil: "domcontentloaded" },
  );
  await expect(page.getByTestId("eval-page")).toBeVisible({ timeout: 30_000 });
}

test("run list groups trials by prompt digest and opens the run detail", async ({ page }) => {
  await mockEvalApi(page, {});
  await openEvalPage(page);

  const list = page.getByTestId("eval-run-list");
  await expect(list).toBeVisible();
  await expect(list.getByTestId("eval-run-group")).toHaveCount(2);
  await expect(list).toContainText("0123456789ab");
  await expect(list).toContainText("eval-prompt.v1");
  await expect(list).toContainText("2 次 trial");
  await expect(list).toContainText("Terminal 均分 3.5");

  const rows = list.getByTestId("eval-run-row");
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText("trial-2");
  await expect(rows.nth(1)).toContainText("trial-1");
  await expect(rows.nth(1)).toContainText("超时");
  await expect(rows.nth(1)).toContainText("Terminal 3.5");
  await expect(rows.nth(1)).toContainText("推荐 A → B");
  await expect(rows.nth(0)).toContainText("运行中");
  await expect(rows.nth(0).getByTestId("eval-reuse-badge")).toHaveText("复用 trial-1 产物");
  await expect(rows.nth(1).getByTestId("eval-reuse-badge")).toHaveCount(0);

  await rows.nth(1).click();
  await expect(page.getByTestId("eval-run-detail")).toBeVisible();
  expect(page.url()).toContain(`run_id=${encodeURIComponent(RUN_ID)}`);

  const detail = page.getByTestId("eval-run-detail");
  await expect(detail.getByTestId("eval-prompt")).toContainText("PROMPT_MARKER");
  await expect(detail.getByTestId("eval-prompt")).toContainText("0123456789ab");
  await expect(detail.getByTestId("eval-prompt")).toContainText("eval-prompt.v1");

  await expect(detail.getByTestId("eval-lane-status-terminal")).toContainText("PRD 已冻结");
  await expect(detail.getByTestId("eval-lane-status-kanban")).toContainText("PRD 已冻结");
  await expect(detail.getByTestId("eval-lane-status-channel")).toContainText("超时");

  await expect(detail.getByTestId("eval-prd-terminal")).toContainText("TERMINAL_PRD_BODY");
  await expect(detail.getByTestId("eval-prd-kanban")).toContainText("KANBAN_PRD_BODY");
  await expect(detail.getByTestId("eval-prd-channel")).toContainText("未产出 PRD");

  const scores = detail.getByTestId("eval-scores");
  await expect(scores).toContainText("从零开发");
  await expect(scores).toContainText("0.92");
  await expect(scores).toContainText("需求覆盖");
  await expect(scores).toContainText("需求忠实");
  await expect(scores).toContainText("可执行性");
  await expect(scores).toContainText("完整性结构");
  await expect(scores).toContainText("综合参考分");
  await expect(scores).toContainText("3.5");
  await expect(scores).toContainText("2.8");
  await expect(scores.locator("tfoot")).toContainText("—");
  await expect(detail.getByTestId("eval-checklist-terminal")).toContainText("需求清单 9/12 通过 · 部分 2 · 缺失 1");
  await expect(detail.getByTestId("eval-checklist-kanban")).toContainText("需求清单 7/12 通过 · 部分 3 · 缺失 2");
  await expect(detail.getByTestId("eval-checklist-channel")).toHaveCount(0);

  const terminalConcerns = detail.getByTestId("eval-concerns-terminal");
  await expect(terminalConcerns).toContainText("疑虑 2 项");
  await expect(terminalConcerns.locator("ul")).not.toBeVisible();
  await terminalConcerns.locator("summary").click();
  await expect(terminalConcerns.locator("li")).toHaveCount(2);
  await expect(terminalConcerns).toContainText("CONCERN_MARKER");
  await expect(detail.getByTestId("eval-concerns-kanban")).toContainText("疑虑 0（已核查）");

  const crossCompare = detail.getByTestId("eval-cross-compare");
  await expect(crossCompare).toContainText("推荐排名");
  await expect(crossCompare.getByTestId("eval-label-legend")).toContainText("A = Terminal");
  await expect(crossCompare.getByTestId("eval-label-legend")).toContainText("B = Kanban");
  const recRanking = crossCompare.getByTestId("eval-rec-ranking");
  await expect(recRanking.locator("li")).toHaveCount(2);
  await expect(recRanking).toContainText("Terminal（A）");
  await expect(recRanking).toContainText("Kanban（B）");

  const dimTable = crossCompare.getByTestId("eval-dim-table");
  await expect(dimTable.locator("[data-testid^='eval-dim-row-']")).toHaveCount(2);
  const coverageRow = dimTable.getByTestId("eval-dim-row-coverage");
  await expect(coverageRow).toContainText("需求覆盖");
  await expect(coverageRow).toContainText("Terminal");
  await expect(coverageRow).toContainText("Kanban");
  await expect(coverageRow).toContainText("极高");
  const fidelityRow = dimTable.getByTestId("eval-dim-row-fidelity");
  await expect(fidelityRow).toContainText("需求忠实");
  await expect(fidelityRow).toContainText("中");

  const rationale = crossCompare.locator(".eval-dim-rationale");
  await expect(rationale).toHaveCount(1);
  await expect(rationale.locator("p")).not.toBeVisible();
  await rationale.locator("summary").click();
  await expect(rationale.locator("p")).toBeVisible();
  await expect(rationale.locator("p")).toContainText("RATIONALE_MARKER");

  const gapGroups = crossCompare.getByTestId("eval-gap-groups");
  await expect(gapGroups.locator("[data-testid^='eval-gap-group-']")).toHaveCount(2);
  const kanbanGaps = gapGroups.getByTestId("eval-gap-group-kanban");
  await expect(kanbanGaps).toContainText("Kanban（B）");
  await expect(kanbanGaps.locator("li")).toHaveCount(2);
  await expect(kanbanGaps).toContainText("KEY_GAP_MARKER");
  await expect(kanbanGaps).toContainText("KEY_GAP_B2");
  const terminalGaps = gapGroups.getByTestId("eval-gap-group-terminal");
  await expect(terminalGaps).toContainText("Terminal（A）");
  await expect(terminalGaps.locator("li")).toHaveCount(1);
  await expect(terminalGaps).toContainText("KEY_GAP_A1");

  await expect(crossCompare.getByTestId("eval-conflicts")).toContainText("CONFLICT_MARKER");

  const reportFold = detail.getByTestId("eval-conclusion");
  await expect(reportFold).toContainText("结论报告");
  await expect(reportFold.locator(".eval-report-body")).not.toBeAttached();
  await reportFold.locator("summary").click();
  await expect(reportFold.locator(".eval-report-body")).toContainText("REPORT_MARKER");

  await detail.getByTestId("eval-back").click();
  await expect(page.getByTestId("eval-run-list")).toBeVisible();
  expect(page.url()).not.toContain("run_id=");
});

test("deep link with run_id opens the detail view directly", async ({ page }) => {
  await mockEvalApi(page, {});
  await openEvalPage(page, RUN_ID);
  const detail = page.getByTestId("eval-run-detail");
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("PROMPT_MARKER");
});

test("list error exposes retry and recovers on the next load", async ({ page }) => {
  let attempts = 0;
  await mockEvalApi(page, {
    list: async (route) => {
      attempts += 1;
      if (attempts === 1) {
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({ error: "temporary" }),
        });
        return;
      }
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(listPayload()),
      });
    },
  });
  await openEvalPage(page);
  await expect(page.getByTestId("eval-run-list")).toContainText("returned 503");
  await page.getByTestId("eval-list-retry").click();
  await expect(page.getByTestId("eval-run-list")).toContainText("0123456789ab");
  expect(attempts).toBeGreaterThanOrEqual(2);
});

test("a benchmark.eval SSE event refreshes the run list", async ({ page }) => {
  let completed = false;
  let listRequests = 0;
  let captureStream!: (route: Route) => void;
  const stream = new Promise<Route>((resolve) => {
    captureStream = resolve;
  });
  await page.route("**/api/projects/*/stream?*", async (route) => {
    if (completed) await route.abort();
    else captureStream(route);
  });
  await mockEvalApi(page, {
    list: async (route) => {
      listRequests += 1;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(listPayload(completed ? "completed" : "running")),
      });
    },
  });
  await openEvalPage(page);
  const firstRow = page.getByTestId("eval-run-row").filter({ hasText: "trial-1" });
  await expect(firstRow.getByText("运行中", { exact: true })).toBeVisible();
  completed = true;
  await (await stream).fulfill({
    contentType: "text/event-stream",
    body: `id: 1000000\ndata: ${JSON.stringify({
      type: "benchmark.eval.run.completed",
      seq: 1_000_000,
      payload: { run_id: RUN_ID },
    })}\n\n`,
  });
  await expect(firstRow.locator(".eval-status-pill")).toContainText("已完成", { timeout: 10_000 });
  expect(listRequests).toBeGreaterThanOrEqual(2);
});

test("eval page keeps narrow layouts within the viewport", async ({ page }) => {
  await mockEvalApi(page, {});
  await openEvalPage(page, RUN_ID);
  await expect(page.getByTestId("eval-run-detail")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
