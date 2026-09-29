import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Next.js 16 blocks cross-origin requests to dev-only resources (JS
  // chunks, HMR) by default. Without this, loading the dev server via
  // 127.0.0.1 (as opposed to localhost) silently blocks the client bundle
  // entirely -- the page shell renders but no client component ever
  // hydrates or runs its effects. Needed for Playwright (which navigates
  // via 127.0.0.1) and for anyone else doing the same locally.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Next.js 16 dev server auto-writes AGENTS.md/CLAUDE.md into this
  // directory on every startup. This repo has its own docs; don't let the
  // dev server regenerate scaffold files here.
  agentRules: false,
};

export default nextConfig;
