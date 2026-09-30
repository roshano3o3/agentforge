import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";
import { PYTHON, REPO_ROOT } from "./env";

const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");

function runCli(args: string[]) {
  execFileSync(PYTHON, ["-m", "agentforge_cli.main", ...args, "--api-url", API_URL], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
}

test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  // Seed one real, persisted evaluation run (via the real CLI, against the
  // same fresh API instance this whole suite uses; the CLI submits it and
  // waits while the e2e worker container executes it) so the Applications
  // and Runs pages have real data -- not just whatever this one test
  // creates through the UI.
  runCli(["dataset", "publish", "datasets/rag_support_v1.yaml"]);
  runCli([
    "evaluate",
    "--app", "rag-assistant",
    "--app-version", "v1",
    "--dataset", "rag-support",
    "--dataset-version", "latest",
    "--adapter", "rag_app.adapter:answer",
  ]);
});

test("dataset version lifecycle through the UI: draft -> edit -> publish -> locked -> new version from it", async ({
  page,
}) => {
  // -- Applications page (seeded by the CLI run above) --
  await page.goto("/applications");
  await expect(page.getByRole("heading", { name: "Applications" })).toBeVisible();
  await expect(page.getByRole("link", { name: "rag-assistant" })).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "applications.png"), fullPage: true });

  // -- Datasets page: list view, then create a brand-new dataset via the UI --
  await page.goto("/datasets");
  await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible();
  await expect(page.getByRole("link", { name: "rag-support" })).toBeVisible();

  const datasetName = "ui-created-dataset";
  await page.getByPlaceholder("Dataset name (e.g. rag-support)").fill(datasetName);
  await page.getByPlaceholder("Description (optional)").fill("Created end-to-end through the Playwright UI test");
  await page.getByRole("button", { name: "Create dataset" }).click();

  const datasetLink = page.getByRole("link", { name: datasetName });
  await expect(datasetLink).toBeVisible();
  await datasetLink.click();
  await expect(page).toHaveURL(new RegExp(`/datasets/${datasetName}$`));

  // -- Create a draft version --
  await page.getByRole("button", { name: "New draft" }).click();
  await expect(page.getByText("v1", { exact: false }).first()).toBeVisible();
  await expect(page.locator(".badge-neutral", { hasText: "DRAFT" })).toBeVisible();

  // -- Add a test case to the draft --
  await page.getByPlaceholder("case_key (e.g. refund-policy-001)").fill("case-one");
  await page.getByPlaceholder("input (the question)").fill("What is the return window?");
  await page.getByPlaceholder("expected_context (comma-separated doc ids)").fill("policy-returns-001");
  await page.getByRole("button", { name: "Add case" }).click();
  await expect(page.getByRole("cell", { name: "case-one" })).toBeVisible();

  // -- Edit that case in place (only possible while the version is a draft) --
  await page.getByRole("button", { name: "Edit" }).click();
  const editInput = page.locator(".version-block input[type=text]").first();
  await editInput.fill("What is the return window, edited?");
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText("What is the return window, edited?")).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "dataset-draft-editing.png"), fullPage: true });

  // -- Publish it --
  await page.getByRole("button", { name: "Publish this version" }).click();
  await expect(page.locator(".badge-pass", { hasText: "PUBLISHED" })).toBeVisible();

  // Confirm it's actually locked: no Edit/Delete controls remain for this
  // version's test cases, and the API rejects a direct PATCH with 409 --
  // proving the lock is real, not just a UI convention.
  await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0);
  const rejectResponse = await page.request.patch(`${API_URL}/datasets/${datasetName}/versions/1`, {
    data: { test_cases: [{ case_key: "case-one", input: "hacked", expected_context: [], tags: [] }] },
  });
  expect(rejectResponse.status()).toBe(409);
  const rejectBody = await rejectResponse.json();
  expect(rejectBody.detail.toLowerCase()).toContain("immutable");

  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "dataset-published-locked.png"), fullPage: true });

  // -- Create v2 from v1: copies the (edited, published) case into a new draft --
  await page.getByRole("button", { name: "Create new version from this" }).click();
  await expect(page.getByText("v2", { exact: false }).first()).toBeVisible();

  const v2Card = page.locator(".version-block", { has: page.getByRole("heading", { name: /^v2\b/ }) });
  await expect(v2Card.locator(".badge-neutral", { hasText: "DRAFT" })).toBeVisible();
  await expect(v2Card.getByRole("cell", { name: "case-one" })).toBeVisible();
  await expect(v2Card.getByText("What is the return window, edited?")).toBeVisible();
  // v1 itself is untouched by creating v2.
  const v1Card = page.locator(".version-block", { has: page.getByRole("heading", { name: /^v1\b/ }) });
  await expect(v1Card.locator(".badge-pass", { hasText: "PUBLISHED" })).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "dataset-v2-from-v1.png"), fullPage: true });

  // Independently verify via the API that this is all real, persisted data.
  const versionsResp = await page.request.get(`${API_URL}/datasets/${datasetName}/versions`);
  const versionsBody = await versionsResp.json();
  expect(versionsBody.map((v: { version: number; status: string }) => [v.version, v.status])).toEqual([
    [2, "draft"],
    [1, "published"],
  ]);

  // -- Runs page: the run the CLI submitted in beforeAll, executed by the worker --
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Evaluation runs" })).toBeVisible();
  const runLink = page.locator("table a.mono").first();
  await expect(runLink).toBeVisible();

  await runLink.click();
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible();
  await expect(page.getByText("heuristic_context_precision", { exact: false }).first()).toBeVisible();
});
