import { defineConfig, devices } from "@playwright/test";
import { API_PORT, WEB_PORT } from "./e2e/ports";
import { PYTHON, REPO_ROOT } from "./e2e/env";

// A dedicated API instance on its own port, pointed at a disposable Postgres
// database and its own Redis queue, with a dedicated worker container
// consuming that queue -- all set up (and torn down) by scripts/test-ui.ps1,
// which passes them in through these env vars. It never touches the dev DB,
// the dev queue, or a manually running `dev-api.ps1` on :8000.
const DATABASE_URL = process.env.AGENTFORGE_E2E_DATABASE_URL;
const REDIS_URL = process.env.AGENTFORGE_E2E_REDIS_URL;
if (!DATABASE_URL || !REDIS_URL) {
  throw new Error(
    "Run the browser suite via .\\scripts\\test-ui.ps1: it needs Postgres, Redis and a worker container " +
      "(AGENTFORGE_E2E_DATABASE_URL / AGENTFORGE_E2E_REDIS_URL).",
  );
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `"${PYTHON}" apps/api/scripts/serve_fresh.py`,
      cwd: REPO_ROOT,
      env: {
        AGENTFORGE_DATABASE_URL: DATABASE_URL,
        AGENTFORGE_REDIS_URL: REDIS_URL,
        // Settings.cors_origins defaults to just :3000; this suite's web
        // server runs on WEB_PORT instead, so the browser's fetch from
        // that origin needs to be explicitly allowed or the API rejects
        // it with CORS (which looks like "can't reach the API" in the UI,
        // not an obvious CORS error -- found via a failing screenshot).
        AGENTFORGE_CORS_ORIGINS: JSON.stringify([`http://127.0.0.1:${WEB_PORT}`, `http://localhost:${WEB_PORT}`]),
        PORT: String(API_PORT),
      },
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
    {
      command: `npx next dev -p ${WEB_PORT}`,
      cwd: __dirname,
      env: {
        NEXT_PUBLIC_API_URL: `http://127.0.0.1:${API_PORT}`,
      },
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
