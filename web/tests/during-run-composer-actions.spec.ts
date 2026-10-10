import { expect, test } from "@playwright/test";

test("during-run composer follows the LibreChat control hierarchy", async ({ page }) => {
  await page.goto("/?fixture=agent-session");
  const composer = page.getByTestId("fx-during-run-composer");
  const input = composer.getByLabel("During-run fixture message");

  await expect(composer.getByRole("button", { name: "Stop generating" })).toBeVisible();
  await expect(composer.getByRole("button", { name: "Steer this response sooner" })).toHaveCount(0);

  await input.fill("Keep the API contract unchanged");
  await expect(composer.getByRole("button", { name: "Stop generating" })).toBeHidden();
  await expect(composer.getByRole("button", { name: "Steer this response sooner" })).toBeVisible();
  const primary = composer.getByRole("button", { name: "Queue message" });
  await expect(primary).toBeVisible();

  const menu = composer.getByRole("menu", { name: "During-run actions" });
  await expect(menu).toBeHidden();
  await primary.hover();
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Steer", exact: true })).toBeDisabled();
  await expect(menu.getByRole("menuitem", { name: /Queue/ })).toBeEnabled();
  await expect(menu.getByRole("menuitem", { name: "Steer sooner" })).toBeEnabled();
  await expect(menu.getByRole("menuitem", { name: "Stop generating" })).toBeEnabled();

  await menu.getByRole("menuitem", { name: /Queue/ }).click();
  await expect(page.getByTestId("during-run-action-echo")).toHaveText("queue");
  await expect(composer.getByRole("button", { name: "Stop generating" })).toBeVisible();
});

test("during-run controls stay icon-sized on mobile and the fast steer action works", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto("/?fixture=agent-session");
  const composer = page.getByTestId("fx-during-run-composer");
  await composer.getByLabel("During-run fixture message").fill("Use the existing store");

  const steerSooner = composer.getByRole("button", { name: "Steer this response sooner" });
  const queue = composer.getByRole("button", { name: "Queue message" });
  const bounds = await Promise.all([steerSooner.boundingBox(), queue.boundingBox()]);
  expect(bounds[0]?.width).toBe(36);
  expect(bounds[1]?.width).toBe(36);
  expect((bounds[1]?.x ?? 0) + (bounds[1]?.width ?? 0)).toBeLessThanOrEqual(390);

  await steerSooner.click();
  await expect(page.getByTestId("during-run-action-echo")).toHaveText("steer-sooner");
  await expect(composer.getByRole("button", { name: "Stop generating" })).toBeVisible();
});
