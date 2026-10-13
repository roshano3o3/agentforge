import fs from "node:fs";
import path from "node:path";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { DEMO_API_URL, VIEWPORT } from "./playwright.config";

// See playwright.config.ts: run against a stack seeded by `agentforge demo seed`. Every capture is the real
// dashboard showing that stack's stored runs; nothing is drawn or edited afterwards.
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
const SCREENSHOTS = path.join(REPO_ROOT, "docs", "screenshots");
const VIDEO_DIR = path.join(__dirname, "..", "test-results", "demo-video");
const CASE = "injection_indirect.memo-delete@get_invoice.contact-lookup-001";

type RunRow = { id: string; application_version: string; dataset_name: string; application_name: string };
type Demo = {
  rag: string;
  v1: string;
  v2: string;
  v1Safety: string;
  v2Safety: string;
  pr4Safety: string;
  caseResult: string;
  replay: string;
};

async function demoIds(request: APIRequestContext): Promise<Demo> {
  const runs: RunRow[] = await (await request.get(`${DEMO_API_URL}/runs`)).json();
  const find = (app: string, version: string, dataset: string): string => {
    const run = runs.find(
      (r) => r.application_name === app && r.application_version === version && r.dataset_name === dataset,
    );
    if (!run) throw new Error(`no ${app}@${version} run on ${dataset}: seed the demo first (agentforge demo seed)`);
    return run.id;
  };
  const pr4Safety = find("invoice-agent", "pr4-d2-off", "invoice-agent-safety");
  const pr4 = await (await request.get(`${DEMO_API_URL}/runs/${pr4Safety}`)).json();
  const caseResult = pr4.results.find((r: { case_key: string }) => r.case_key === CASE).id;
  const replays = await (await request.get(`${DEMO_API_URL}/results/${caseResult}/replays`)).json();
  return {
    rag: find("rag-assistant", "v1", "rag-support"),
    v1: find("invoice-agent", "v1", "invoice-agent"),
    v2: find("invoice-agent", "v2", "invoice-agent"),
    v1Safety: find("invoice-agent", "v1", "invoice-agent-safety"),
    v2Safety: find("invoice-agent", "v2", "invoice-agent-safety"),
    pr4Safety,
    caseResult,
    replay: replays[replays.length - 1].id, // the seed's (oldest)
  };
}

async function shot(page: Page, file: string): Promise<void> {
  await page.mouse.move(0, 0); // no hover highlight
  await page.screenshot({ path: path.join(SCREENSHOTS, file), fullPage: true });
}

/** One case's result row + trajectory, measured and captured in the same layout (see e2e/trajectory-flow). */
async function shotCase(page: Page, caseKey: string, file: string): Promise<void> {
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  await page.setViewportSize({ width: VIEWPORT.width, height });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.mouse.move(0, 0);
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
  await page.screenshot({ path: path.join(SCREENSHOTS, file), clip: box });
  await page.setViewportSize(VIEWPORT);
}

async function openTrajectory(page: Page, caseKey: string): Promise<void> {
  const trajectory = page.locator(`details[data-trajectory-for="${caseKey}"]`);
  await trajectory.locator("summary").click();
  await expect(trajectory.getByRole("list", { name: `Trajectory for ${caseKey}` })).toBeVisible();
}

test.describe.configure({ mode: "serial" });

test("screenshots", async ({ page }) => {
  fs.mkdirSync(SCREENSHOTS, { recursive: true });
  const ids = await demoIds(page.request);

  await page.goto("/runs");
  await expect(page.locator(`tr[data-run-id="${ids.v2}"]`)).toBeVisible();
  await shot(page, "runs.png");

  await page.getByRole("button", { name: "New run" }).click();
  const form = page.getByRole("form", { name: "Start a run" });
  await form.getByRole("combobox", { name: "Application", exact: true }).selectOption({ label: "invoice-agent" });
  await form.getByRole("combobox", { name: "Dataset", exact: true }).selectOption({ label: "invoice-agent" });
  await expect(form.getByRole("combobox", { name: "Published version", exact: true })).toContainText("10 cases");
  await shot(page, "run-new-form.png"); // not submitted

  await page.goto(`/runs/${ids.rag}`);
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible();
  await shot(page, "run-detail-completed.png");

  await page.goto(`/runs/${ids.v1}`);
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible();
  await openTrajectory(page, "refund-small-001");
  await shotCase(page, "refund-small-001", "trajectory-v1-passing.png");

  await page.goto(`/runs/${ids.v2}`);
  await expect(page.locator("h1 .badge", { hasText: "completed" })).toBeVisible();
  await openTrajectory(page, "refund-over-limit-001");
  await expect(page.locator('details[data-trajectory-for="refund-over-limit-001"] li[data-step="3"]')).toHaveClass(
    /step-failing/,
  );
  await shotCase(page, "refund-over-limit-001", "trajectory-v2-failing.png");
  await openTrajectory(page, "refund-small-001");
  await shot(page, "run-detail-trajectory-v2.png");

  await page.goto("/baselines");
  await expect(page.locator('tr[data-baseline="invoice-agent:production:invoice-agent"]')).toBeVisible();
  await shot(page, "baselines.png");

  await page.goto(`/regression?baseline=${ids.v1}&candidate=${ids.v2}`);
  await expect(page.locator(".decision h3")).toContainText("FAILED");
  await shot(page, "regression-v1-vs-v2.png");

  await page.goto(`/safety?run=${ids.v2Safety}&baseline=${ids.v1Safety}`);
  const indirect = page.locator('tr[data-category="injection_indirect"]');
  await indirect.getByRole("button", { name: "5 failing ▸" }).click();
  await page.locator(`details[data-case-key="${CASE}"] > summary`).click();
  await expect(page.locator(`details[data-case-key="${CASE}"] li[data-step="2"]`)).toHaveClass(/step-failing/);
  await shot(page, "safety-v1-vs-v2.png");

  await page.goto(`/traces/${ids.caseResult}`);
  await expect(page.locator('tr[data-span-name="execute_tool delete_invoice"]')).toHaveClass(/span-failing/);
  await shot(page, "trace-explorer.png");

  await page.goto(`/replays/${ids.replay}`);
  await expect(page.locator('[data-verdict="after"]')).toHaveText("PASS");
  await shot(page, "replay-diff.png");

  await page.goto("/applications");
  await expect(page.getByRole("link", { name: "invoice-agent" })).toBeVisible();
  await shot(page, "applications.png");

  await page.goto("/datasets");
  await expect(page.getByRole("link", { name: "invoice-agent-safety" })).toBeVisible();
  await shot(page, "datasets.png");

  await page.goto("/datasets/rag-support");
  await expect(page.locator(".badge-pass", { hasText: "PUBLISHED" }).first()).toBeVisible();
  await shot(page, "dataset-published-locked.png");
});

test("dataset draft screenshots (creates a draft v2 of rag-support on the stack it runs against)", async ({
  page,
}) => {
  // Last, because it changes the data: a published version can only be "edited" as a new draft.
  await page.goto("/datasets/rag-support");
  await page.getByRole("button", { name: "Create new version from this" }).click();
  const v2 = page.locator(".version-block", { has: page.getByRole("heading", { name: /^v2\b/ }) });
  await expect(v2.locator(".badge-neutral", { hasText: "DRAFT" })).toBeVisible();
  await shot(page, "dataset-v2-from-v1.png");

  await v2.getByRole("button", { name: "Edit" }).first().click();
  const input = v2.locator("input[type=text]").first();
  await input.fill(`${await input.inputValue()} (draft edit)`);
  await v2.getByRole("button", { name: "Save" }).click();
  await expect(v2.getByText("(draft edit)")).toBeVisible();
  await shot(page, "dataset-draft-editing.png");
});

test("demo video: runs -> regression -> safety -> trace -> replay with D2 on", async ({ browser }) => {
  fs.rmSync(VIDEO_DIR, { recursive: true, force: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    colorScheme: "light",
    recordVideo: { dir: VIDEO_DIR, size: { width: 1280, height: 800 } },
  });
  const page = await context.newPage();
  const ids = await demoIds(page.request);
  const pause = (ms: number) => page.waitForTimeout(ms);
  const scrollTo = async (selector: string) => {
    await page.locator(selector).first().evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
    await pause(700);
  };

  // 1. Runs list
  await page.goto("/runs");
  await expect(page.locator(`tr[data-run-id="${ids.v2}"]`)).toBeVisible();
  await pause(2000);

  // 2. Regression: v1 (production baseline) vs v2, the stored release decision FAILED
  await page.getByRole("link", { name: "Regression", exact: true }).click();
  await page.goto(`/regression?baseline=${ids.v1}&candidate=${ids.v2}`);
  await expect(page.locator(".decision h3")).toContainText("FAILED");
  await pause(1400);
  await scrollTo('details[data-case-class="newly_failing"]');
  await pause(900);
  await scrollTo(".decision");
  await pause(1700);

  // 3. Safety dashboard: demo PR #4 (D2 off) vs v1, injection_indirect drill-down
  await page.getByRole("link", { name: "Safety", exact: true }).click();
  await page.goto(`/safety?run=${ids.pr4Safety}&baseline=${ids.v1Safety}`);
  const indirect = page.locator('tr[data-category="injection_indirect"]');
  await expect(indirect).toContainText("20% (1/5)");
  await pause(1700);
  await indirect.getByRole("button", { name: "4 failing ▸" }).click();
  const failing = page.locator(`details[data-case-key="${CASE}"]`);
  await failing.locator(":scope > summary").click();
  await scrollTo(`details[data-case-key="${CASE}"]`);
  await pause(1700);

  // 4. Trace Explorer: delete_invoice (step 2) blamed in red
  await failing.getByRole("link", { name: "Open trace →" }).click();
  const del = page.locator('tr[data-span-name="execute_tool delete_invoice"]');
  await expect(del).toHaveClass(/span-failing/);
  await pause(1100);
  await scrollTo('tr[data-span-name="execute_tool delete_invoice"]');
  await pause(1700);

  // 5. Replay with D2 on
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await pause(500);
  await page.getByRole("button", { name: "Replay" }).click();
  const form = page.getByRole("form", { name: "Replay this case" });
  await form.evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
  await pause(900);
  await form.getByRole("checkbox", { name: /^d2 — D2: treat tool output as data/ }).check();
  await pause(900);
  await form.getByRole("button", { name: "Replay with overrides" }).click();

  // 6. FAIL -> PASS, injection_resisted fixed, delete_invoice removed
  await expect(page.locator('[data-verdict="after"]')).toHaveText("PASS", { timeout: 60_000 });
  await pause(1700);
  await scrollTo('tr[data-evaluator="injection_resisted"]');
  await pause(1400);
  await scrollTo('tr[data-op="removed"]');
  await pause(2400);

  await context.close(); // flushes the video
  const videos = fs.readdirSync(VIDEO_DIR).filter((f) => f.endsWith(".webm"));
  expect(videos).toHaveLength(1);
  fs.renameSync(path.join(VIDEO_DIR, videos[0]), path.join(VIDEO_DIR, "demo.webm"));
});
