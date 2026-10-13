import { defineConfig, devices } from "@playwright/test";

// Captures docs/screenshots/*.png and the demo video (-> docs/demo.gif) from a running, SEEDED stack:
// `docker compose up` (which runs `agentforge demo seed` into an empty database), then
// `.\scripts\demo-capture.ps1`. Not part of the test suite: it starts no servers and asserts only what it
// needs to find its way around. Light theme, one window size for every capture.
export const DEMO_URL = process.env.AGENTFORGE_DEMO_URL ?? "http://127.0.0.1:3000";
export const DEMO_API_URL = process.env.AGENTFORGE_DEMO_API_URL ?? "http://127.0.0.1:8000";
export const VIEWPORT = { width: 1440, height: 900 };

export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "../test-results/demo",
  timeout: 180_000,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: DEMO_URL,
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    colorScheme: "light",
  },
});
