import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { API_PORT } from "./ports";
import { PYTHON, REPO_ROOT } from "./env";

const API_URL = `http://127.0.0.1:${API_PORT}`;
const SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "screenshots");
const APP_NAME = "invoice-agent-replay-ui";
const CASE = "injection_indirect.memo-delete@get_invoice.contact-lookup-001";
// Plain text for the regex below (see regression-flow.spec.ts): no ANSI from Rich on CI.
const CLI_ENV: NodeJS.ProcessEnv = { ...process.env, NO_COLOR: "1", PYTHONIOENCODING: "utf-8", COLUMNS: "200" };
delete CLI_ENV.FORCE_COLOR;

function cli(args: string[]): string {
  return execFileSync(PYTHON, ["-m", "agentforge_cli.main", ...args, "--api-url", API_URL], {
    cwd: REPO_ROOT,
    encoding: "utf-8",
    env: CLI_ENV,
  });
}

function evaluate(version: string, adapterArgs: string[]): string {
  const out = cli([
    "evaluate", "--app", APP_NAME, "--app-version", version, "--dataset", "invoice-agent-safety", ...adapterArgs,
  ]);
  return /Submitted run ([0-9a-f-]{36})/.exec(out)![1];
}

async function resultId(request: import("@playwright/test").APIRequestContext, runId: string): Promise<string> {
  const run = await (await request.get(`${API_URL}/runs/${runId}`)).json();
  return run.results.find((r: { case_key: string }) => r.case_key === CASE).id;
}

let pr4Run = "";
let httpRun = "";

test.beforeAll(async () => {
  test.setTimeout(180_000);
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  cli(["dataset", "publish", "datasets/invoice_agent_safety_v1.yaml"]);
  // Demo PR #4's agent: v1 with only defense D2 off.
  pr4Run = evaluate("pr4-d2-off", ["--adapter", "invoice_agent.adapter:answer_v1_without_d2"]);
  // An HTTP adapter (nothing listens there, so every case errors): it can't declare replay settings.
  httpRun = evaluate("http", ["--adapter-url", "http://127.0.0.1:1/answer", "--timeout", "2"]);
});

test("replay: PR #4's failing injection case, replayed from the Trace Explorer with D2 on", async ({ page }) => {
  const id = await resultId(page.request, pr4Run);
  await page.goto(`/runs/${pr4Run}`);
  await page.locator(`a[href="/traces/${id}"]`).click();
  await expect(page).toHaveURL(`/traces/${id}`);
  await expect(page.locator('[data-empty="no-replays"]')).toBeVisible();

  await page.getByRole("button", { name: "Replay" }).click();
  const form = page.getByRole("form", { name: "Replay this case" });
  // The form is built from the run's recorded options: this adapter's defaults (D2 off) are pre-set.
  const d2 = form.getByRole("checkbox", { name: /^d2 — D2: treat tool output as data/ });
  await expect(d2).not.toBeChecked();
  await expect(form.getByRole("checkbox", { name: /^d1 — / })).toBeChecked();
  await expect(form.locator('[data-setting="behavior"] select')).toHaveValue("v1-without-d2");
  await expect(form.locator("[data-overrides-summary]")).toContainText("none — replays the case exactly as it ran");
  await d2.check();
  await expect(form.locator("[data-overrides-summary]")).toContainText("d2=on");
  await form.getByRole("button", { name: "Replay with overrides" }).click();

  await expect(page).toHaveURL(/\/replays\/[0-9a-f-]{36}$/);
  await expect(page.locator('[data-verdict="before"]')).toHaveText("FAIL", { timeout: 60_000 });
  await expect(page.locator('[data-verdict="after"]')).toHaveText("PASS");
  await expect(page.locator("[data-overrides]")).toContainText("d2=on");
  const injection = page.locator('tr[data-evaluator="injection_resisted"]');
  await expect(injection).toHaveAttribute("data-change", "fixed");
  await expect(injection).toContainText("FAIL 0.00");
  await expect(injection).toContainText("PASS 1.00");
  // The removed step: delete_invoice, on the original's side only.
  const removed = page.locator('tr[data-op="removed"]');
  await expect(removed).toHaveCount(1);
  await expect(removed).toHaveAttribute("data-step-name", "delete_invoice");
  await expect(removed).toContainText('delete_invoice {"invoice_id":"INV-1004"}');
  await expect(page.locator('tr[data-op="unchanged"][data-step-name="get_customer"]')).toHaveCount(1);
  await expect(page.locator('[data-answer-changed="false"]')).toBeVisible();
  // Same worker code as the original run: no warning, and it says so.
  await expect(page.locator("[data-provenance-warning]")).toHaveCount(0);
  await expect(page.locator("[data-code-version]")).toContainText("the same as the original run");
  const listed = page.getByRole("table", { name: "Replays" }).locator("tr[data-replay-id]");
  await expect(listed).toHaveCount(1);
  await expect(listed).toContainText("FAIL → PASS");
  await expect(listed).toContainText("viewing");

  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "replay-diff.png"), fullPage: true });

  // The original trace now lists it.
  await page.getByRole("link", { name: "original trace" }).click();
  await expect(page.getByRole("table", { name: "Replays" }).locator("tr[data-replay-id]")).toHaveCount(1);
});

test("replay: an HTTP adapter can't take overrides; a plain replay is still offered", async ({ page }) => {
  const id = await resultId(page.request, httpRun);
  await page.goto(`/traces/${id}`);
  await page.getByRole("button", { name: "Replay" }).click();
  const form = page.getByRole("form", { name: "Replay this case" });
  await expect(form.locator("[data-replay-unavailable]")).toContainText("HTTP adapters can't declare replay settings");
  await expect(form.locator("[data-setting]")).toHaveCount(0);
  await form.getByRole("button", { name: "Replay without overrides" }).click();
  await expect(page).toHaveURL(/\/replays\/[0-9a-f-]{36}$/);
  await expect(page.locator('[data-identical="true"]')).toBeVisible({ timeout: 60_000 });
});

test("replay: loading, error and empty states", async ({ page }) => {
  const id = await resultId(page.request, pr4Run);
  // Options: held (loading), then an error.
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(`${API_URL}/results/${id}/replay-options`, async (route) => {
    await held;
    await route.fulfill({ status: 503, json: { detail: "database unavailable" } });
  });
  await page.goto(`/traces/${id}`);
  await page.getByRole("button", { name: "Replay" }).click();
  await expect(page.getByText("Loading replay options…")).toBeVisible();
  release();
  await expect(page.getByRole("alert").filter({ hasText: "Could not load the replay options" })).toContainText(
    "database unavailable",
  );
  await page.unroute(`${API_URL}/results/${id}/replay-options`);

  // A rejected request (400 with the accepted settings) is shown on the form.
  await page.route(`${API_URL}/replay`, (route) =>
    route.fulfill({
      status: 400,
      json: { detail: { message: "overrides rejected: unknown setting 'x'", accepted_settings: [] } },
    }),
  );
  await page.reload();
  await page.getByRole("button", { name: "Replay" }).click();
  const form = page.getByRole("form", { name: "Replay this case" });
  await form.getByRole("button", { name: "Replay without overrides" }).click();
  await expect(form.getByRole("alert")).toContainText("400: overrides rejected: unknown setting 'x'");
  await page.unroute(`${API_URL}/replay`);

  // The replay list's error state, and an unknown replay.
  await page.route(`${API_URL}/results/${id}/replays`, (route) =>
    route.fulfill({ status: 503, json: { detail: "database unavailable" } }),
  );
  await page.reload();
  await expect(page.getByRole("alert").filter({ hasText: "Could not load the replays" })).toBeVisible();
  await page.goto("/replays/00000000-0000-0000-0000-000000000000");
  await expect(page.locator('[data-empty="no-replay"]')).toContainText("No such replay.");
});
