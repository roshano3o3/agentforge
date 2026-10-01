import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";
import { PYTHON, REPO_ROOT } from "./env";

const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");
const APP_NAME = "ui-run-app";

test.beforeAll(async ({ request }) => {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  // A published dataset (via the real CLI) and an application with a version
  // (via the real API) for the form to pick. No run is created here: the
  // test starts it from the UI.
  execFileSync(PYTHON, ["-m", "agentforge_cli.main", "dataset", "publish", "datasets/rag_support_v1.yaml", "--api-url", API_URL], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
  const app = await (await request.post(`${API_URL}/applications`, { data: { name: APP_NAME } })).json();
  await request.post(`${API_URL}/applications/${app.id}/versions`, { data: { version: "ui-v1" } });
});

test("start a run from the UI and see the worker's completed per-case results", async ({ page }) => {
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Evaluation runs" })).toBeVisible();

  await page.getByRole("button", { name: "New run" }).click();
  const form = page.getByRole("form", { name: "Start a run" });
  await expect(form).toBeVisible();

  // Role + accessible name, not getByLabel: a <label> wrapping a <select>
  // includes the options' text, so exact label matching never hits.
  const select = (name: string) => form.getByRole("combobox", { name, exact: true });
  await select("Application").selectOption({ label: APP_NAME });
  await expect(select("App version")).toHaveValue(/.+/);
  await select("Dataset").selectOption({ label: "rag-support" });
  await expect(select("Published version")).toContainText("5 cases");
  await expect(form.getByRole("textbox", { name: "Adapter target" })).toHaveValue("rag_app.adapter:answer");
  // Every registered evaluator is pre-selected: 9 answer/retrieval/measurement + 7 trajectory + 5 safety.
  await expect(form.getByRole("checkbox")).toHaveCount(21);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "run-new-form.png"), fullPage: true });

  await form.getByRole("button", { name: "Start run" }).click();

  // Lands on the run's page, which follows pending -> running -> completed.
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const runId = page.url().split("/").pop()!;
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible({ timeout: 60_000 });

  await expect(page.getByText("Fixture-based results.")).toBeVisible();
  await expect(page.locator("h1 .badge-label", { hasText: "fixture-based" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Per-metric means" })).toBeVisible();

  // Per-case rows: each case shows only its configured evaluators, with verdicts and reasons.
  const rows = page.locator("tr[data-case-key]");
  await expect(rows).toHaveCount(5);
  const shipping = page.locator('tr[data-case-key="shipping-time-001"]');
  const shippingContains = shipping.locator(".metric-chip", { hasText: "answer_contains" });
  await expect(shippingContains).toContainText("pass");
  await expect(shippingContains.locator(".metric-reason")).toContainText("contains all 2 expected phrase");
  // A genuine failure: the refund answer never states the tags requirement.
  const refundContains = page
    .locator('tr[data-case-key="refund-policy-001"]')
    .locator(".metric-chip", { hasText: "answer_contains" });
  await expect(refundContains).toContainText("fail");
  await expect(refundContains.locator(".metric-reason")).toContainText("missing 1 of 1");
  // The out-of-scope case drops the precision/recall defaults and uses a refusal check instead.
  const outOfScope = page.locator('tr[data-case-key="out-of-scope-sponsorship-001"]');
  await expect(outOfScope.locator(".metric-chip", { hasText: "heuristic_context_precision" })).toHaveCount(0);
  await expect(outOfScope.locator(".metric-chip", { hasText: "answer_regex" })).toContainText("pass");
  await expect(page.getByText("est. $", { exact: false }).first()).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "run-detail-completed.png"), fullPage: true });

  // The page shows exactly what the API persisted.
  const run = await (await page.request.get(`${API_URL}/runs/${runId}`)).json();
  expect(run.status).toBe("completed");
  expect(run.adapter_type).toBe("python");
  expect(run.results).toHaveLength(5);
  expect(run.aggregates.case_count).toBe(5);

  // And the list shows it with its stored aggregates.
  await page.goto("/runs");
  const listRow = page.locator(`tr[data-run-id="${runId}"]`);
  await expect(listRow.locator(".badge", { hasText: "completed" })).toBeVisible();
  await expect(listRow).toContainText("5/5");
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "runs.png"), fullPage: true });
});
