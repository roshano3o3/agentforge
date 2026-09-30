// Mirrors apps/api/agentforge_api's Pydantic response schemas field-for-field
// (packages/core/agentforge_core/schemas.py is the source of truth).

export type RunStatus = "pending" | "running" | "completed" | "failed";
export type ResultStatus = "ok" | "error" | "timeout";

export const FIXTURE_BASED = "fixture-based";

export interface RunProgress {
  completed_cases: number;
  total_cases: number;
}

export interface MetricAggregate {
  name: string;
  version: string;
  unit: string | null;
  mean_score: number | null;
  mean_value: number | null;
  scored_count: number;
  valued_count: number;
  pass_rate: number | null;
  not_applicable_count: number;
}

// Computed once by the worker when a run finishes (definitions in
// packages/evaluators/agentforge_evaluators/aggregate.py).
export interface RunAggregates {
  case_count: number;
  ok_count: number;
  error_count: number;
  timeout_count: number;
  passed_count: number;
  pass_rate: number | null;
  latency_ms: { basis: string; p50: number | null; p95: number | null; mean: number | null };
  estimated_cost_usd: {
    label: "estimated";
    total: number | null;
    cases_with_estimate: number;
    cases_without_estimate: number;
  } | null;
  metrics: Record<string, MetricAggregate>;
}

export interface EvaluationRunSummary {
  id: string;
  application_name: string;
  application_version: string;
  dataset_name: string;
  dataset_version: number;
  provider_type: string;
  labels: string[];
  evaluators: string[];
  status: RunStatus;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  progress: RunProgress;
  aggregates: RunAggregates | null;
}

export interface MetricScore {
  evaluator_name: string;
  evaluator_version: string;
  score: number | null;
  value: number | null;
  unit: string | null;
  passed: boolean | null;
  reason: string;
  evidence: Record<string, unknown>;
  labels: string[];
}

export interface EvaluationResult {
  id: string;
  test_case_id: string;
  case_key: string;
  input: string;
  expected_answer: string | null;
  expected_context: string[];
  retrieved_doc_ids: string[];
  citations: string[];
  output_answer: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  model: string | null;
  latency_ms: number;
  status: ResultStatus;
  passed: boolean | null;
  error_type: string | null;
  error_message: string | null;
  metrics: MetricScore[];
}

export interface EvaluationRun extends EvaluationRunSummary {
  application_id: string;
  application_version_id: string;
  dataset_version_id: string;
  adapter_type: string | null;
  adapter_target: string | null;
  environment: string;
  threshold: number;
  max_latency_ms: number | null;
  timeout_seconds: number | null;
  git_commit_sha: string | null;
  results: EvaluationResult[];
}

export interface Evaluator {
  name: string;
  version: string;
  key: string;
  kind: "quality" | "measurement";
  description: string;
}

export interface RunCreateInput {
  application_id: string;
  application_version_id: string;
  dataset_version_id: string;
  adapter: { type: "python" | "http"; target: string };
  // null = apply each case's configured evaluators; a list additionally filters them.
  evaluators: string[] | null;
  threshold: number;
  timeout_seconds: number;
  provider_type: string;
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

// {"<evaluator name or name@version>": {params} | false}; see docs/evaluators.md.
export type EvaluatorConfig = Record<string, Record<string, unknown> | boolean>;

export interface TestCase {
  id: string;
  case_key: string;
  input: string;
  expected_answer: string | null;
  expected_context: string[];
  tags: string[];
  evaluators: EvaluatorConfig | null;
  // Read-only legacy fields on versions published before per-case config.
  expected_answer_contains: string[];
  expected_answer_regex: string | null;
}

export type DatasetVersionStatus = "draft" | "published";

export interface DatasetVersion {
  id: string;
  dataset_id: string;
  version: number;
  status: DatasetVersionStatus;
  created_at: string;
  published_at: string | null;
  default_evaluators: EvaluatorConfig | null;
  test_cases: TestCase[];
}

// Shape of the "add a test case" form. Not a wire type -- each field gets
// converted into a TestCaseIn (expected_context/tags split on commas) when
// sent as part of a full-replace PATCH.
export interface DraftTestCaseForm {
  case_key: string;
  input: string;
  expected_answer: string;
  expected_context: string; // comma-separated in the form, split on submit
  tags: string; // comma-separated in the form, split on submit
}
