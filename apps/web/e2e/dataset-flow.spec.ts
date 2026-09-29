import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";

const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");

function runCli(args: string[]) {
  execFileSync(VENV_PYTHON, ["-m", "agentforge_cli.main", ...args, "--api-url", API_URL], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    env: { ...process.env, PYTHONIOENCODING: "utf-8" },
  });
}

test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  // Seed one real, persisted evaluation run (via the real CLI, against the
  // same fresh API instance this whole suite uses) so the Applications,
  // Datasets, and Runs pages all have real data to screenshot -- not just
  // whatever this one test creates through the UI.
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

test("dataset can be created and published through the UI, and editing a published version is rejected", async ({
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

  // -- Dataset detail page: compose two test cases, then publish --
  await expect(page).toHaveURL(new RegExp(`/datasets/${datasetName}$`));
  await expect(page.getByRole("heading", { name: datasetName })).toBeVisible();

  async function addDraftCase(caseKey: string, input: string, expectedContext: string) {
    await page.getByPlaceholder("case_key (e.g. refund-policy-001)").fill(caseKey);
    await page.getByPlaceholder("input (the question)").fill(input);
    await page.getByPlaceholder("expected_context (comma-separated doc ids)").fill(expectedContext);
    await page.getByRole("button", { name: "Add to draft" }).click();
  }

  await addDraftCase("case-one", "What is the return window?", "policy-returns-001");
  await addDraftCase("case-two", "Is shipping ever free?", "policy-shipping-002");

  // Both drafted rows show up in the pre-publish table before anything is persisted.
  await expect(page.getByText("case-one")).toBeVisible();
  await expect(page.getByText("case-two")).toBeVisible();

  await page.getByRole("button", { name: /Publish version 1/ }).click();

  // Published version now shows both test cases, read from the real API response.
  await expect(page.getByText("v1", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "case-one" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "case-two" })).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "dataset-detail.png"), fullPage: true });

  // -- Immutability: attempt to edit the published version through the UI,
  // and confirm the real API rejection (409) is what's shown, not a
  // simulated one. --
  await page.getByRole("button", { name: "Try to edit" }).first().click();
  await page.locator(".edit-attempt-panel input").fill("an edited answer that should never be saved");
  await page.getByRole("button", { name: "Send PATCH to API" }).click();

  const resultPanel = page.locator(".edit-attempt-panel .state-box");
  await expect(resultPanel).toContainText("HTTP 409");
  await expect(resultPanel).toContainText(/immutable/i);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "dataset-edit-rejected.png"), fullPage: true });

  // Prove the rejection was real, not just a UI message: re-fetch the
  // version directly from the API and confirm the content is untouched.
  const versionResponse = await page.request.get(`${API_URL}/datasets/${datasetName}/versions/1`);
  const versionBody = await versionResponse.json();
  const caseOne = versionBody.test_cases.find((tc: { case_key: string }) => tc.case_key === "case-one");
  expect(caseOne.input).toBe("What is the return window?");

  // -- Runs page: the run seeded in beforeAll, with real persisted data --
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "Evaluation runs" })).toBeVisible();
  const runLink = page.locator("table a.mono").first();
  await expect(runLink).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "runs.png"), fullPage: true });

  await runLink.click();
  await expect(page.getByText("heuristic_context_precision", { exact: false }).first()).toBeVisible();
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "run-detail.png"), fullPage: true });
});
