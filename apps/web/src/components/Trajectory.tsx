"use client";

import { useState } from "react";
import type { AgentStep, EvaluationResult, MetricScore } from "@/lib/types";

// Long args/results collapse to this many lines (or characters) behind "show more".
const PREVIEW_LINES = 6;
const PREVIEW_CHARS = 280;

interface Flag {
  evaluator: string;
  reason: string;
}

/** A failed evaluator's `evidence.failing_steps` (1-based step numbers), or []. */
function failingSteps(metric: MetricScore): number[] {
  const raw = metric.evidence?.failing_steps;
  return Array.isArray(raw) ? raw.filter((n): n is number => typeof n === "number") : [];
}

/** Which steps each failed evaluator blamed, and the failures it couldn't pin
 * to a step (e.g. "get_invoice never called"). */
function flags(metrics: MetricScore[]): { byStep: Map<number, Flag[]>; unplaced: Flag[] } {
  const byStep = new Map<number, Flag[]>();
  const unplaced: Flag[] = [];
  for (const m of metrics) {
    if (m.passed !== false) continue;
    const flag = { evaluator: m.evaluator_name, reason: m.reason };
    const steps = failingSteps(m);
    if (steps.length === 0) unplaced.push(flag);
    for (const n of steps) byStep.set(n, [...(byStep.get(n) ?? []), flag]);
  }
  return { byStep, unplaced };
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/** Shows `value` in full when short; otherwise a preview with "show more". */
export function Collapsible({ label, value, tone }: { label: string; value: unknown; tone?: "error" }) {
  const [open, setOpen] = useState(false);
  const text = asText(value);
  const lines = text.split("\n");
  const long = lines.length > PREVIEW_LINES || text.length > PREVIEW_CHARS;
  const shown = !long || open ? text : `${lines.slice(0, PREVIEW_LINES).join("\n").slice(0, PREVIEW_CHARS)}…`;
  return (
    <div className={`step-field${tone === "error" ? " step-field-error" : ""}`}>
      <span className="step-field-label">{label}</span>
      <pre className="step-value">{shown}</pre>
      {long && (
        <button type="button" className="link-button" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "show less" : "show more"}
        </button>
      )}
    </div>
  );
}

function StepBody({ step }: { step: AgentStep }) {
  if (step.kind === "tool_call") {
    return (
      <>
        <Collapsible label="args" value={step.args} />
        {step.error !== null ? (
          <Collapsible label="error" value={step.error} tone="error" />
        ) : (
          <Collapsible label="result" value={step.result} />
        )}
      </>
    );
  }
  if (step.kind === "retrieval") {
    return <Collapsible label="retrieved" value={step.retrieved_doc_ids} />;
  }
  return <Collapsible label="output" value={step.output ?? ""} />;
}

function StepItem({ step, stepFlags }: { step: AgentStep; stepFlags: Flag[] }) {
  const failing = stepFlags.length > 0;
  return (
    <li className={`step${failing ? " step-failing" : ""}`} data-step={step.step_index} data-failing={failing}>
      <div className="step-number" aria-label={`Step ${step.step_index}`}>
        {step.step_index}
      </div>
      <div className="step-main">
        <div className="step-head">
          <span className={`step-kind step-kind-${step.kind}`}>{step.kind.replace("_", " ")}</span>
          {step.name && <span className="mono step-tool">{step.name}</span>}
          {step.duration_ms !== null && <span className="meta-line"> {step.duration_ms.toFixed(1)} ms</span>}
        </div>
        <StepBody step={step} />
      </div>
      <div className="step-flags">
        {stepFlags.map((f) => (
          <div key={f.evaluator} className="step-flag" role="note">
            <span className="mono">{f.evaluator}</span>
            <span className="step-flag-reason">{f.reason}</span>
          </div>
        ))}
      </div>
    </li>
  );
}

/** One case's trajectory: every step the agent reported, in order, with the
 * steps a failed evaluator blamed highlighted and its reason beside them. */
/** `defaultOpen`: start expanded (the Safety page's drill-down is there to show the blamed step). */
export default function Trajectory({ result, defaultOpen = false }: { result: EvaluationResult; defaultOpen?: boolean }) {
  const { byStep, unplaced } = flags(result.metrics);
  const flaggedCount = byStep.size;

  if (result.status !== "ok") {
    return (
      <div className="trajectory-empty">
        No trajectory recorded: the adapter {result.status === "timeout" ? "timed out" : "raised an error"} before
        returning one.
      </div>
    );
  }
  if (result.steps.length === 0) {
    return <div className="trajectory-empty">The agent reported no steps for this case.</div>;
  }
  return (
    // Collapsed by default; the summary already says how many steps were flagged.
    <details className="trajectory" data-trajectory-for={result.case_key} open={defaultOpen}>
      <summary>
        Trajectory · {result.steps.length} step{result.steps.length === 1 ? "" : "s"}
        {flaggedCount > 0 ? (
          <span className="trajectory-flagged"> · {flaggedCount} flagged</span>
        ) : (
          <span className="meta-line"> · no step flagged</span>
        )}
      </summary>
      <ol className="steps" aria-label={`Trajectory for ${result.case_key}`}>
        {result.steps.map((step) => (
          <StepItem key={step.step_index} step={step} stepFlags={byStep.get(step.step_index) ?? []} />
        ))}
      </ol>
      {unplaced.length > 0 && (
        <div className="trajectory-unplaced">
          <div className="step-field-label">Failures not tied to a single step</div>
          {unplaced.map((f) => (
            <div key={f.evaluator} className="step-flag" role="note">
              <span className="mono">{f.evaluator}</span>
              <span className="step-flag-reason">{f.reason}</span>
            </div>
          ))}
        </div>
      )}
    </details>
  );
}
