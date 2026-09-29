import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import os from "node:os";
import { API_PORT, WEB_PORT } from "./e2e/ports";

// A dedicated API instance on its own port + its own temp SQLite file, so
// this suite never touches the shared dev DB (agentforge_dev.db) or
// collides with a manually-running `dev-api.ps1` on :8000. Playwright
// starts both servers, waits for their health checks, and tears them down
// after the run.
const REPO_ROOT = path.resolve(__dirname, "..", "..");
const VENV_PYTHON = path.join(REPO_ROOT, ".venv", "Scripts", "python.exe");
const TEMP_DB = path.join(os.tmpdir(), `agentforge_playwright_${Date.now()}.db`);

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
      command: `${VENV_PYTHON} apps/api/scripts/serve_fresh.py`,
      cwd: REPO_ROOT,
      env: {
        AGENTFORGE_DATABASE_URL: `sqlite+aiosqlite:///${TEMP_DB.replace(/\\/g, "/")}`,
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
      timeout: 30_000,
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
