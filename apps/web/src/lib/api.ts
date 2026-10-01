import type {
  Application,
  ApplicationVersion,
  Dataset,
  DatasetVersion,
  EvaluationRun,
  EvaluationRunSummary,
  Evaluator,
  EvaluatorConfig,
  RunCreateInput,
  TrajectoryExpectations,
} from "./types";

// Overridable via NEXT_PUBLIC_API_URL for non-default setups; defaults to
// the AgentForge API's localhost-only address.
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

/** Creates a *pending* run and queues it for the worker. Poll getRun for progress. */
export function createRun(input: RunCreateInput): Promise<EvaluationRun> {
  return post<EvaluationRun>("/runs", input);
}

export function listEvaluators(): Promise<Evaluator[]> {
  return apiFetch<Evaluator[]>("/evaluators");
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
  evaluators: EvaluatorConfig | null;
  trajectory: TrajectoryExpectations | null;
}

/** Creates a new DRAFT version (optionally pre-populated). Does not publish it. */
export function createDraftVersion(datasetId: string, testCases: TestCaseInput[] = []): Promise<DatasetVersion> {
  return post<DatasetVersion>(`/datasets/${datasetId}/versions`, { test_cases: testCases });
}

/** Replaces a draft's entire test-case set. Rejected with 409 if the
 * version is published -- the API enforces this, not the client. */
export function patchDraftVersion(
  datasetName: string,
  version: number,
  testCases: TestCaseInput[],
): Promise<DatasetVersion> {
  return patch<DatasetVersion>(`/datasets/${encodeURIComponent(datasetName)}/versions/${version}`, {
    test_cases: testCases,
  });
}

/** One-way: draft -> published. Never reversed. */
export function publishVersion(datasetName: string, version: number): Promise<DatasetVersion> {
  return post<DatasetVersion>(`/datasets/${encodeURIComponent(datasetName)}/versions/${version}/publish`, undefined);
}

/** Creates a new draft (next version number) copying `version`'s test
 * cases -- the supported way to "edit" a published version's content. */
export function newDraftFromVersion(datasetName: string, version: number): Promise<DatasetVersion> {
  return post<DatasetVersion>(
    `/datasets/${encodeURIComponent(datasetName)}/versions/${version}/new-draft`,
    undefined,
  );
}
