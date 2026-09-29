import type {
  Application,
  ApplicationVersion,
  Dataset,
  DatasetVersion,
  EvaluationRun,
  EvaluationRunSummary,
  TestCase,
} from "./types";

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

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      cache: "no-store",
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
      ...init,
    });
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
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "POST", body: JSON.stringify(body) });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}

// -- runs ---------------------------------------------------------------

export function listRuns(): Promise<EvaluationRunSummary[]> {
  return apiFetch<EvaluationRunSummary[]>("/runs");
}

export function getRun(runId: string): Promise<EvaluationRun> {
  return apiFetch<EvaluationRun>(`/runs/${runId}`);
}

// -- applications ---------------------------------------------------------

export function listApplications(): Promise<Application[]> {
  return apiFetch<Application[]>("/applications");
}

export function getApplication(name: string): Promise<Application> {
  return apiFetch<Application>(`/applications/${encodeURIComponent(name)}`);
}

export function createApplication(name: string, description: string | null): Promise<Application> {
  return post<Application>("/applications", { name, description });
}

export function listApplicationVersions(applicationId: string): Promise<ApplicationVersion[]> {
  return apiFetch<ApplicationVersion[]>(`/applications/${applicationId}/versions`);
}

export function createApplicationVersion(
  applicationId: string,
  version: string,
  description: string | null,
): Promise<ApplicationVersion> {
  return post<ApplicationVersion>(`/applications/${applicationId}/versions`, { version, description });
}

// -- datasets ---------------------------------------------------------------

export function listDatasets(): Promise<Dataset[]> {
  return apiFetch<Dataset[]>("/datasets");
}

export function createDataset(name: string, description: string | null): Promise<Dataset> {
  return post<Dataset>("/datasets", { name, description });
}

export function getDataset(name: string): Promise<Dataset> {
  return apiFetch<Dataset>(`/datasets/${encodeURIComponent(name)}`);
}

export function listDatasetVersions(name: string): Promise<DatasetVersion[]> {
  return apiFetch<DatasetVersion[]>(`/datasets/${encodeURIComponent(name)}/versions`);
}

export interface TestCaseInput {
  case_key: string;
  input: string;
  expected_answer: string | null;
  expected_context: string[];
  tags: string[];
}

export function publishDatasetVersion(datasetId: string, testCases: TestCaseInput[]): Promise<DatasetVersion> {
  return post<DatasetVersion>(`/datasets/${datasetId}/versions`, { test_cases: testCases });
}

/** Always rejected by the API with 409 -- published versions are immutable.
 * Exists so the UI can demonstrate that rejection explicitly rather than
 * just not offering an edit control. */
export function attemptEditPublishedVersion(datasetName: string, version: number, testCase: TestCase): Promise<void> {
  return patch<void>(`/datasets/${encodeURIComponent(datasetName)}/versions/${version}`, {
    test_cases: [
      {
        case_key: testCase.case_key,
        input: testCase.input,
        expected_context: testCase.expected_context,
        tags: testCase.tags,
      },
    ],
  });
}
