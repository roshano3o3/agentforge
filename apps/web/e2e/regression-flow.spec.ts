import { test, expect } from "@playwright/test";
import { execFileSync, spawnSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";
import { PYTHON, REPO_ROOT } from "./env";

const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");
const APP_NAME = "invoice-agent-gate-ui";
const CLI_ENV = { ...process.env, PYTHONIOENCODING: "utf-8", COLUMNS: "200" };

function cli(args: string[]): string {
  return execFileSync(PYTHON, ["-m", "agentforge_cli.main", ...args, "--api-url", API_URL], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    env: CLI_ENV,
  });
}

function evaluate(version: "v1" | "v2"): string {
  const out = cli([
    "evaluate", "--app", APP_NAME, "--app-version", version, "--dataset", "invoice-agent",
    "--adapter", `invoice_agent.adapter:answer_${version}`,
  ]);
  return /Submitted run ([0-9a-f-]{36})/.exec(out)![1];
}

let baselineRun = "";
let candidateRun = "";

test.beforeAll(async () => {
  test.setTimeout(180_000);
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  // Everything through the real CLI and the e2e worker: two runs, the
  // production baseline pointer, and a release decision made by `gate`.
  cli(["dataset", "publish", "datasets/invoice_agent_v1.yaml"]);
  baselineRun = evaluate("v1");
  candidateRun = evaluate("v2");
  cli(["baseline", "set", baselineRun, "--env", "production"]);
  const gate = spawnSync(
    PYTHON,
    ["-m", "agentforge_cli.main", "gate", "--candidate", candidateRun, "--baseline", "production", "--api-url", API_URL],
    { cwd: REPO_ROOT, encoding: "utf-8", env: CLI_ENV },
  );
  expect(gate.status).toBe(1); // FAILED, as the policy in agentforge.yaml requires for v2
  expect(gate.stdout.trimEnd().split("\n").pop()).toBe("RELEASE GATE: FAILED");
});

test("baselines and regression: pointer, deltas with markers, case classes, the release decision", async ({ page }) => {
  // -- Baselines: the current pointer per environment ------------------------------
  await page.goto("/baselines");
  await expect(page.getByRole("heading", { name: "Baselines" })).toBeVisible();
  const row = page.locator(`tr[data-baseline="${APP_NAME}:production"]`);
  await expect(row).toBeVisible();
  await expect(row).toContainText("v1");
  await expect(row).toContainText("100%");
  await expect(row.getByRole("link", { name: baselineRun.slice(0, 8) })).toHaveAttribute("href", `/runs/${baselineRun}`);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "baselines.png"), fullPage: true });

  // -- Regression: from the baseline, pick the v2 candidate --------------------------
  await row.getByRole("link", { name: "Compare a run against it →" }).click();
  await expect(page).toHaveURL(new RegExp(`/regression\\?baseline=${baselineRun}`));
  const form = page.getByRole("form", { name: "Choose runs to compare" });
  await expect(form.getByRole("combobox", { name: "Baseline run" })).toHaveValue(baselineRun);
  await form.getByRole("combobox", { name: "Candidate run" }).selectOption(candidateRun);

  // Run-level: pass rate down 60 points, marked worse.
  const passRate = page.getByRole("table", { name: "Run-level deltas" }).locator('tr[data-metric="pass_rate"]');
  await expect(passRate).toContainText("100%");
  await expect(passRate).toContainText("40%");
  const marker = passRate.locator(".delta");
  await expect(marker).toHaveAttribute("data-direction", "worse");
  await expect(marker).toContainText("▼ -60.0 pts (-60.0%)");
  // No errors in either run: unchanged.
  await expect(page.locator('tr[data-metric="error_rate"] .delta-same')).toHaveText("= 0");

  // Per evaluator: approval_required's pass rate went from 100% to 0%.
  const approval = page.locator('tr[data-evaluator="approval_required"]');
  await expect(approval.locator(".delta").nth(1)).toHaveAttribute("data-direction", "worse");
  await expect(approval.locator(".delta").nth(1)).toContainText("-100.0 pts");

  // Cases, classified.
  const newly = page.locator('details[data-case-class="newly_failing"]');
  await expect(newly.locator("summary")).toContainText("Newly failing 6");
  const refund = newly.locator("li", { hasText: "refund-small-001" });
  await expect(refund.locator(".badge-critical")).toHaveText("critical");
  await expect(refund).toContainText("failed: approval_required, sequence_order, tool_args, tool_selection");
  await expect(page.locator('details[data-case-class="still_passing"] summary')).toContainText("Still passing 4");
  await expect(page.locator('details[data-case-class="fixed"] summary')).toContainText("Fixed 0");

  // The release decision `agentforge gate` recorded, with each check.
  await expect(page.locator(".decision h3")).toContainText("FAILED");
  const checks = page.getByRole("table", { name: "Release gate checks" });
  const minPass = checks.locator('tr[data-check="minimum:pass_rate"]');
  await expect(minPass).toContainText("FAIL");
  await expect(minPass).toContainText("0.4 < 0.9");
  await expect(checks.locator('tr[data-check="cases:newly_failing[tag=critical]"]')).toContainText(
    "newly failing 'critical' case(s): refund-over-limit-001, refund-small-001, void-duplicate-001",
  );
  await expect(checks.locator('tr[data-check="maximum:error_rate"]')).toContainText("pass");
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "regression-v1-vs-v2.png"), fullPage: true });

  // What the page shows is what the API stored.
  const [decision] = await (await page.request.get(`${API_URL}/release-decisions?candidate_run_id=${candidateRun}`)).json();
  expect(decision.passed).toBe(false);
  expect(decision.baseline_run_id).toBe(baselineRun);
});

test("regression refuses runs of different dataset versions", async ({ page }) => {
  // A run of another dataset (the RAG run seeded by dataset-flow.spec.ts, or any non-invoice run).
  const runs = await (await page.request.get(`${API_URL}/runs`)).json();
  const other = runs.find((r: { dataset_name: string; status: string }) => r.dataset_name !== "invoice-agent" && r.status === "completed");
  expect(other).toBeTruthy();
  await page.goto(`/regression?baseline=${baselineRun}&candidate=${other.id}`);
  // (Next.js renders its own empty role="alert" route announcer, so filter by text.)
  const alert = page.getByRole("alert").filter({ hasText: "compare" });
  await expect(alert).toContainText("Can't compare these runs.");
  await expect(alert).toContainText("different dataset versions");
});
