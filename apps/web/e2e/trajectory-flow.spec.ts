import { test, expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";
import { PYTHON, REPO_ROOT } from "./env";

const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");
const APP_NAME = "invoice-agent-ui";

test.beforeAll(async ({ request }) => {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  // The trajectory dataset (via the real CLI) and an application with both
  // agent versions (via the real API). Runs are started from the UI.
  execFileSync(
    PYTHON,
    ["-m", "agentforge_cli.main", "dataset", "publish", "datasets/invoice_agent_v1.yaml", "--api-url", API_URL],
    { cwd: REPO_ROOT, encoding: "utf-8", env: { ...process.env, PYTHONIOENCODING: "utf-8" } },
  );
  const app = await (await request.post(`${API_URL}/applications`, { data: { name: APP_NAME } })).json();
  for (const version of ["v1", "v2"]) {
    await request.post(`${API_URL}/applications/${app.id}/versions`, { data: { version } });
  }
});

/** Starts a run of the invoice agent from the New run form and waits for the worker to finish it. */
async function startRun(page: Page, appVersion: "v1" | "v2"): Promise<string> {
  await page.goto("/runs");
  await page.getByRole("button", { name: "New run" }).click();
  const form = page.getByRole("form", { name: "Start a run" });
  const select = (name: string) => form.getByRole("combobox", { name, exact: true });
  await select("Application").selectOption({ label: APP_NAME });
  await expect(select("App version")).toContainText(appVersion);
  await select("App version").selectOption({ label: appVersion });
  await select("Dataset").selectOption({ label: "invoice-agent" });
  await expect(select("Published version")).toContainText("10 cases");
  await form.getByRole("textbox", { name: "Adapter target" }).fill(`invoice_agent.adapter:answer_${appVersion}`);
  await form.getByRole("button", { name: "Start run" }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("tr[data-case-key]")).toHaveCount(10);
  return page.url().split("/").pop()!;
}

/** Screenshots one case (its result row + trajectory timeline). The viewport
 * is first grown to the page's full height, so the case is measured and
 * captured in the same layout: a fullPage capture resizes the page after it
 * was measured, and the first screenshots came out shifted by a line. */
async function screenshotCase(page: Page, caseKey: string, file: string): Promise<void> {
  const original = page.viewportSize()!;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: original.width, height });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.mouse.move(0, 0); // no hover highlight on the row
  const box = await page.locator(`tbody[data-case="${caseKey}"]`).evaluate((el) => {
    const rows = Array.from(el.querySelectorAll(":scope > tr")).map((r) => r.getBoundingClientRect());
    const top = Math.min(...rows.map((r) => r.top));
    const left = Math.min(...rows.map((r) => r.left));
    return {
      x: left,
      y: top,
      width: Math.max(...rows.map((r) => r.right)) - left,
      height: Math.max(...rows.map((r) => r.bottom)) - top,
    };
  });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, file), clip: box });
  await page.setViewportSize(original);
}

/** Opens one case's trajectory timeline and returns its locators. */
async function openTrajectory(page: Page, caseKey: string) {
  const block = page.locator(`tbody[data-case="${caseKey}"]`);
  const trajectory = block.locator(`details[data-trajectory-for="${caseKey}"]`);
  await trajectory.locator("summary").click();
  const steps = trajectory.getByRole("list", { name: `Trajectory for ${caseKey}` });
  await expect(steps).toBeVisible();
  return { trajectory, steps };
}

test("v1 trajectory: every step shown in order, none flagged, long results collapsed", async ({ page }) => {
  const runId = await startRun(page, "v1");
  await expect(page.locator('tr[data-case-key] .badge-fail')).toHaveCount(0);

  const { trajectory, steps } = await openTrajectory(page, "refund-small-001");
  await expect(trajectory.locator("summary")).toContainText("4 steps");
  await expect(trajectory.locator("summary")).toContainText("no step flagged");
  const items = steps.locator("li.step");
  await expect(items).toHaveCount(4);
  await expect(items.locator(".step-tool")).toHaveText(["get_invoice", "request_human_approval", "issue_refund"]);
  await expect(steps.locator("li.step-failing")).toHaveCount(0);
  await expect(items.nth(1)).toContainText('"approved": true');
  await expect(items.nth(2)).toContainText('"amount": 40');
  await expect(items.nth(3)).toContainText("Refunded 40.00 USD on INV-1001");
  await screenshotCase(page, "refund-small-001", "trajectory-v1-passing.png");

  // A long result is collapsed behind "show more", and expands in place.
  const history = await openTrajectory(page, "payment-history-001");
  const result = history.steps.locator('li[data-step="1"] .step-field', { hasText: "result" });
  await expect(result.locator("pre")).not.toContainText("2026-09-26");
  await result.getByRole("button", { name: "show more" }).click();
  await expect(result.locator("pre")).toContainText("2026-09-26");
  await expect(result.getByRole("button", { name: "show less" })).toBeVisible();

  // The page shows what the API persisted.
  const run = await (await page.request.get(`${API_URL}/runs/${runId}`)).json();
  expect(run.aggregates.pass_rate).toBe(1);
});

test("v2 trajectory: the failing step is highlighted with the evaluator's reason beside it", async ({ page }) => {
  const runId = await startRun(page, "v2");

  // refund-over-limit-001: approval was denied at step 2, and v2 refunded anyway at step 3.
  const failingRow = page.locator('tr[data-case-key="refund-over-limit-001"]');
  await expect(failingRow.locator(".badge", { hasText: "fail" })).toBeVisible();
  const { trajectory, steps } = await openTrajectory(page, "refund-over-limit-001");
  await expect(trajectory.locator("summary")).toContainText("1 flagged");

  const step3 = steps.locator('li[data-step="3"]');
  await expect(step3).toHaveClass(/step-failing/);
  await expect(step3.locator(".step-tool")).toHaveText("issue_refund");
  await expect(step3.locator(".step-number")).toHaveCSS("background-color", "rgb(196, 52, 43)");
  const approvalFlag = step3.locator(".step-flag", { hasText: "approval_required" });
  await expect(approvalFlag).toBeVisible();
  await expect(approvalFlag).toContainText(
    "issue_refund called at step 3 after request_human_approval at step 2 was denied",
  );
  await expect(step3.locator(".step-flag", { hasText: "forbidden_tool_use" })).toContainText(
    "issue_refund (forbidden) called at step 3",
  );
  // The denial itself (step 2) is shown, but isn't the step that broke the rule.
  const step2 = steps.locator('li[data-step="2"]');
  await expect(step2).not.toHaveClass(/step-failing/);
  await expect(step2).toContainText('"approved": false');
  await screenshotCase(page, "refund-over-limit-001", "trajectory-v2-failing.png");

  // Another regression: a refund under $100 skipped approval and sent the amount as a string.
  const small = await openTrajectory(page, "refund-small-001");
  const smallStep2 = small.steps.locator('li[data-step="2"]');
  await expect(smallStep2).toHaveClass(/step-failing/);
  await expect(smallStep2.locator(".step-flag", { hasText: "tool_args" })).toContainText(
    "amount: '40.00' is not of type 'number'",
  );
  // A failure with no single step to blame is listed under the timeline, not lost.
  await expect(small.trajectory.locator(".trajectory-unplaced")).toContainText("tool_selection");

  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "run-detail-trajectory-v2.png"), fullPage: true });

  const run = await (await page.request.get(`${API_URL}/runs/${runId}`)).json();
  expect(run.aggregates.passed_count).toBe(4);
  expect(run.aggregates.case_count).toBe(10);
});
