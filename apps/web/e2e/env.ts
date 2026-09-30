import path from "node:path";

export const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

// The repo's virtualenv Python: .venv\Scripts\python.exe on Windows,
// .venv/bin/python elsewhere (CI). AGENTFORGE_PYTHON overrides both.
export const PYTHON =
  process.env.AGENTFORGE_PYTHON ??
  (process.platform === "win32"
    ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
    : path.join(REPO_ROOT, ".venv", "bin", "python"));
