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
  // Per attack category, for datasets with adversarial cases (Phase 5).
  safety?: {
    basis: string;
    cases: number;
    by_category: Record<string, { cases: number; passed: number; pass_rate: number }>;
  } | null;
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

export type StepKind = "retrieval" | "tool_call" | "final_answer";

// One step the agent reported, exactly as stored. step_index is 1-based over
// all steps: the "step N" in evaluator reasons and evidence.failing_steps.
export interface AgentStep {
  step_index: number;
  kind: StepKind;
  name: string;
  args: Record<string, unknown>;
  result: unknown;
  error: string | null;
  retrieved_doc_ids: string[];
  output: string | null;
  duration_ms: number | null;
}

// A test case's trajectory expectations (see docs/evaluators.md).
export type TrajectoryExpectations = Record<string, unknown>;
// Adversarial cases: the environment setup sent to the adapter, and the
// safety block (category, source case, expectations) only evaluators read.
export type Scenario = Record<string, unknown>;
export type SafetyBlock = { category: string; source_case?: string; technique?: string } & Record<string, unknown>;

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
  trajectory: TrajectoryExpectations | null;
  steps: AgentStep[];
  safety?: SafetyBlock | null;
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
  dataset_content_hash?: string | null;
  results: EvaluationResult[];
}

export interface Evaluator {
  name: string;
  version: string;
  key: string;
  kind: "quality" | "measurement" | "trajectory" | "safety";
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
  trajectory: TrajectoryExpectations | null;
  scenario?: Scenario | null;
  safety?: SafetyBlock | null;
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
  content_hash?: string | null;
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

// -- baselines, regression, release gate (Phase 4) ------------------------------

export interface Baseline {
  application_id: string;
  application_name: string;
  environment: string;
  run_id: string;
  set_at: string;
  application_version: string;
  dataset_name: string;
  dataset_version: number;
  pass_rate: number | null;
}

export interface MetricDelta {
  baseline: number | null;
  candidate: number | null;
  delta: number | null; // candidate - baseline
  delta_pct: number | null; // of |baseline|; null when the baseline is 0 or missing
}

export interface EvaluatorDelta {
  name: string;
  unit: string | null;
  baseline_version: string | null;
  candidate_version: string | null;
  comparable: boolean;
  mean_score: MetricDelta;
  pass_rate: MetricDelta;
  mean_value: MetricDelta;
}

export type CaseClass = "newly_failing" | "fixed" | "still_failing" | "still_passing";

export interface CaseChange {
  case_key: string;
  tags: string[];
  baseline_status: ResultStatus;
  candidate_status: ResultStatus;
  candidate_failed_evaluators: string[];
}

export interface RegressionReport {
  baseline_run_id: string;
  candidate_run_id: string;
  dataset_version_id: string;
  summary: Record<string, MetricDelta>;
  metrics: EvaluatorDelta[];
  // Pass rate per attack category (adversarial datasets; empty otherwise).
  safety: Record<string, MetricDelta>;
  cases: Record<CaseClass, CaseChange[]>;
  case_counts: Record<CaseClass, number>;
}

export interface GateCheck {
  kind: "minimum" | "maximum" | "regression" | "cases";
  metric: string;
  rule: string;
  threshold: number | null;
  baseline: number | null;
  candidate: number | null;
  delta: number | null;
  delta_pct: number | null;
  passed: boolean;
  reason: string;
}

export interface ReleaseDecision {
  id: string;
  application_id: string;
  application_name: string;
  candidate_run_id: string;
  baseline_run_id: string;
  baseline_ref: string;
  policy: Record<string, unknown>;
  passed: boolean;
  checks: GateCheck[];
  regression: RegressionReport;
  created_at: string;
}

// GET /traces/{result_id}: one evaluated case's stored span tree (PII redacted
// unless the server was told otherwise).
export interface TraceSpan {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  service: string;
  start_time: string;
  end_time: string;
  duration_ms: number;
  attributes: Record<string, unknown>;
  status_code: "UNSET" | "OK" | "ERROR";
  status_message: string | null;
  events: { name: string; time: string; attributes: Record<string, unknown> }[];
  step_index: number | null;
  children: TraceSpan[];
}

export interface Trace {
  result_id: string;
  case_key: string;
  run_id: string;
  trace_id: string;
  span_count: number;
  spans: TraceSpan[];
}

// -- failure replay (Phase 7) -----------------------------------------------------------

export type ReplaySettingKind = "bool" | "int" | "float" | "str" | "choice" | "text" | "object";

/** One setting an adapter accepts as a replay override (agentforge_sdk.replay.Setting). */
export interface ReplaySetting {
  name: string;
  kind: ReplaySettingKind;
  description: string;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
  max_length: number | null;
  fields: ReplaySetting[];
  /** What the adapter uses when it isn't overridden (null: not stated). */
  default: unknown;
}

export interface ReplayOptions {
  result_id: string;
  run_id: string;
  case_key: string;
  adapter_type: string | null;
  adapter_target: string | null;
  /** False for runs that didn't record their adapter's declaration. */
  recorded: boolean;
  overrides_supported: boolean;
  reason: string | null;
  settings: ReplaySetting[];
  /** The adapter also checks combinations of settings, when the replay runs. */
  worker_checks: boolean;
}

export type ReplayStatus = "pending" | "running" | "completed" | "failed";

export interface ReplaySummary {
  id: string;
  original_result_id: string;
  original_run_id: string;
  case_key: string;
  overrides: Record<string, unknown>;
  status: ReplayStatus;
  error_message: string | null;
  result_status: ResultStatus | null;
  passed: boolean | null;
  original_passed: boolean | null;
  identical: boolean | null;
  created_at: string;
  completed_at: string | null;
}

export interface MetricBrief {
  evaluator_version: string;
  passed: boolean | null;
  score: number | null;
  value: number | null;
  unit: string | null;
  reason: string;
}

export interface EvaluatorChange {
  evaluator_name: string;
  change: "unchanged" | "fixed" | "broken" | "changed" | "added" | "removed";
  before: MetricBrief | null;
  after: MetricBrief | null;
}

export interface StepBrief {
  step_index: number;
  kind: string;
  name: string;
  args: Record<string, unknown>;
  result: unknown;
  error: string | null;
  output: string | null;
  retrieved_doc_ids: string[];
}

export interface StepChange {
  op: "unchanged" | "changed" | "added" | "removed";
  kind: string;
  name: string;
  before: StepBrief | null;
  after: StepBrief | null;
  changed_fields: string[];
}

export interface NumberChange {
  before: number | null;
  after: number | null;
  delta: number | null;
}

export interface ReplayDiff {
  identical: boolean;
  differences: string[];
  before_status: ResultStatus;
  after_status: ResultStatus;
  before_passed: boolean | null;
  after_passed: boolean | null;
  evaluators: EvaluatorChange[];
  trajectory: StepChange[];
  answer: {
    before: string | null;
    after: string | null;
    changed: boolean;
    segments: { op: "equal" | "insert" | "delete"; text: string }[];
  };
  latency_ms: NumberChange;
  input_tokens: NumberChange;
  output_tokens: NumberChange;
}

export interface Provenance {
  code_version: string | null;
  code_sha256: string | null;
  pricing_sha256: string | null;
}

export interface Replay extends ReplaySummary {
  test_case_id: string;
  input: string;
  dataset_version_id: string;
  dataset_content_hash: string | null;
  adapter_type: string;
  adapter_target: string;
  evaluators: string[];
  labels: string[];
  output_answer: string | null;
  latency_ms: number | null;
  error_type: string | null;
  case_error_message: string | null;
  started_at: string | null;
  provenance: {
    original: Provenance;
    replay: Provenance;
    same_code: boolean | null;
    same_pricing: boolean | null;
    warnings: string[];
  };
  diff: ReplayDiff | null;
}
