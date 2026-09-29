import type { EvaluationRun, EvaluationRunSummary } from "./types";

// Overridable via NEXT_PUBLIC_API_URL for non-default setups; defaults to
// the AgentForge API's localhost-only Phase 1 address.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(
    message: string,
    public status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function apiFetch<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new ApiError(
      `Could not reach the AgentForge API at ${API_URL}. Is it running? (see README "Start the services")`,
    );
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      // response body wasn't JSON; fall back to statusText
    }
    throw new ApiError(`${response.status}: ${detail}`, response.status);
  }
  return (await response.json()) as T;
}

export function listRuns(): Promise<EvaluationRunSummary[]> {
  return apiFetch<EvaluationRunSummary[]>("/runs");
}

export function getRun(runId: string): Promise<EvaluationRun> {
  return apiFetch<EvaluationRun>(`/runs/${runId}`);
}
