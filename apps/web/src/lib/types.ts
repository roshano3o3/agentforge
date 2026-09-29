// Mirrors apps/api/agentforge_api's Pydantic response schemas field-for-field
// (packages/core/agentforge_core/schemas.py is the source of truth).

export type RunStatus = "running" | "completed" | "failed";
export type ResultStatus = "ok" | "error";

export interface EvaluationRunSummary {
  id: string;
  application_name: string;
  application_version: string;
  dataset_name: string;
  dataset_version: number;
  evaluator_name: string;
  evaluator_version: string;
  provider_type: string;
  status: RunStatus;
  created_at: string;
  completed_at: string | null;
  case_count: number;
  mean_score: number | null;
  pass_rate: number | null;
  avg_latency_ms: number | null;
}

export interface EvaluationResult {
  id: string;
  test_case_id: string;
  case_key: string;
  input: string;
  expected_context: string[];
  retrieved_doc_ids: string[];
  output_answer: string | null;
  score: number | null;
  passed: boolean | null;
  evidence: Record<string, unknown>;
  latency_ms: number;
  status: ResultStatus;
  error_message: string | null;
}

export interface EvaluationRun extends EvaluationRunSummary {
  application_id: string;
  application_version_id: string;
  dataset_version_id: string;
  environment: string;
  threshold: number;
  git_commit_sha: string | null;
  results: EvaluationResult[];
}

export interface Application {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
}

export interface ApplicationVersion {
  id: string;
  application_id: string;
  version: string;
  description: string | null;
  created_at: string;
}

export interface Dataset {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
}

export interface TestCase {
  id: string;
  case_key: string;
  input: string;
  expected_answer: string | null;
  expected_context: string[];
  tags: string[];
}

export interface DatasetVersion {
  id: string;
  dataset_id: string;
  version: number;
  published_at: string;
  test_cases: TestCase[];
}

// What the UI composes client-side, before publishing turns it into a real
// immutable DatasetVersion. Not a wire type -- nothing this shape is ever
// sent as-is; publish() converts each draft row into a TestCaseIn.
export interface DraftTestCase {
  case_key: string;
  input: string;
  expected_answer: string;
  expected_context: string; // comma-separated in the form, split on publish
  tags: string; // comma-separated in the form, split on publish
}
