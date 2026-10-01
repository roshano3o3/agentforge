"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { ApiError, getRegression, listReleaseDecisions, listRuns } from "@/lib/api";
import { formatMs, formatPct } from "@/lib/format";
import type {
  CaseChange,
  CaseClass,
  EvaluationRunSummary,
  GateCheck,
  MetricDelta,
  RegressionReport,
  ReleaseDecision,
} from "@/lib/types";

type Direction = "higher" | "lower";
type LoadState = "idle" | "loading" | "ready" | "error";

// Which way is better, per metric (same table as agentforge_evaluators/release.py).
const RUN_METRICS: { key: string; label: string; direction: Direction }[] = [
  { key: "pass_rate", label: "Pass rate", direction: "higher" },
  { key: "error_rate", label: "Error rate", direction: "lower" },
  { key: "p50_latency_ms", label: "P50 latency", direction: "lower" },
  { key: "p95_latency_ms", label: "P95 latency", direction: "lower" },
  { key: "estimated_cost_usd", label: "Estimated cost (USD)", direction: "lower" },
];

const CASE_CLASSES: { key: CaseClass; label: string; tone: string }[] = [
  { key: "newly_failing", label: "Newly failing", tone: "worse" },
  { key: "fixed", label: "Fixed", tone: "better" },
  { key: "still_failing", label: "Still failing", tone: "neutral" },
  { key: "still_passing", label: "Still passing", tone: "neutral" },
];

function formatValue(metric: string, value: number | null): string {
  if (value === null) return "-";
  if (metric.endsWith("_ms")) return formatMs(value);
  if (metric.endsWith("rate")) return formatPct(value);
  if (metric === "estimated_cost_usd") return `est. $${value.toFixed(6)}`;
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}

/** ▲/▼ with "better"/"worse" coloring by the metric's direction; = when unchanged. */
function DeltaMarker({ d, direction, metric }: { d: MetricDelta; direction: Direction; metric: string }) {
  if (d.delta === null) return <span className="meta-line">n/a</span>;
  if (Math.abs(d.delta) < 1e-12) return <span className="delta delta-same">= 0</span>;
  const up = d.delta > 0;
  const better = up === (direction === "higher");
  const size = Math.abs(d.delta);
  const shownAbs = `${up ? "+" : "-"}${metric.endsWith("rate") ? `${(size * 100).toFixed(1)} pts` : formatValue(metric, size)}`;
  return (
    <span className={`delta ${better ? "delta-better" : "delta-worse"}`} data-direction={better ? "better" : "worse"}>
      <span aria-label={up ? "up" : "down"}>{up ? "▲" : "▼"}</span> {shownAbs}
      {d.delta_pct !== null && <span className="meta-line"> ({d.delta_pct > 0 ? "+" : ""}{d.delta_pct.toFixed(1)}%)</span>}
    </span>
  );
}

function runLabel(r: EvaluationRunSummary): string {
  const rate = r.aggregates?.pass_rate;
  return (
    `${r.application_name}@${r.application_version} · ${r.dataset_name} v${r.dataset_version} · ` +
    `${rate === null || rate === undefined ? "-" : formatPct(rate)} · ${r.id.slice(0, 8)} · ` +
    new Date(r.created_at).toLocaleString()
  );
}

function CaseList({ cls, cases }: { cls: (typeof CASE_CLASSES)[number]; cases: CaseChange[] }) {
  return (
    <details className={`case-class case-${cls.tone}`} open={cls.key !== "still_passing"} data-case-class={cls.key}>
      <summary>
        {cls.label} <span className="badge badge-neutral">{cases.length}</span>
      </summary>
      {cases.length === 0 ? (
        <div className="meta-line">none</div>
      ) : (
        <ul>
          {cases.map((c) => (
            <li key={c.case_key}>
              <span className="mono">{c.case_key}</span>
              {c.tags.map((t) => (
                <span key={t} className={`badge badge-label${t === "critical" ? " badge-critical" : ""}`}>
                  {t}
                </span>
              ))}
              {c.candidate_failed_evaluators.length > 0 && (
                <span className="meta-line"> — failed: {c.candidate_failed_evaluators.join(", ")}</span>
              )}
              {c.candidate_status !== "ok" && <span className="badge badge-error">{c.candidate_status}</span>}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}

/** 4 significant digits, like the CLI: 25.571184000000358 -> 25.57. */
function short(value: number | null): string {
  return value === null ? "-" : String(Number(value.toPrecision(4)));
}

function Decision({ decision }: { decision: ReleaseDecision }) {
  return (
    <div className="decision" data-decision-id={decision.id}>
      <h3>
        Release gate:{" "}
        <span className={`badge ${decision.passed ? "badge-pass" : "badge-fail"}`}>
          {decision.passed ? "PASSED" : "FAILED"}
        </span>
      </h3>
      <p className="meta-line">
        Decision <span className="mono">{decision.id}</span> · baseline &apos;{decision.baseline_ref}&apos; · recorded{" "}
        {new Date(decision.created_at).toLocaleString()} · immutable. {decision.checks.filter((c) => c.passed).length} of{" "}
        {decision.checks.length} checks passed.
      </p>
      <div className="table-scroll">
        <table aria-label="Release gate checks">
          <thead>
            <tr>
              <th>Result</th>
              <th>Check</th>
              <th>Metric</th>
              <th>Baseline</th>
              <th>Candidate</th>
              <th>Delta</th>
              <th>Threshold</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {decision.checks.map((c: GateCheck, i) => (
              <tr key={i} className={c.passed ? "" : "check-failed"} data-check={`${c.kind}:${c.metric}`}>
                <td>
                  <span className={`badge ${c.passed ? "badge-pass" : "badge-fail"}`}>{c.passed ? "pass" : "FAIL"}</span>
                </td>
                <td>{c.kind}</td>
                <td className="mono">{c.metric}</td>
                <td>{short(c.baseline)}</td>
                <td>{short(c.candidate)}</td>
                <td>
                  {c.delta === null ? "-" : `${c.delta > 0 ? "+" : ""}${short(c.delta)}`}
                  {c.delta_pct !== null && ` (${c.delta_pct > 0 ? "+" : ""}${c.delta_pct.toFixed(1)}%)`}
                </td>
                <td className="mono">{c.rule}</td>
                <td>{c.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RegressionView() {
  const router = useRouter();
  const params = useSearchParams();
  const baselineId = params.get("baseline") ?? "";
  const candidateId = params.get("candidate") ?? "";

  const [runs, setRuns] = useState<EvaluationRunSummary[]>([]);
  const [runsState, setRunsState] = useState<LoadState>("loading");
  const [runsError, setRunsError] = useState("");
  // The comparison for one (baseline, candidate) pair; loading/idle are derived from it.
  const [result, setResult] = useState<
    { key: string; report: RegressionReport; decisions: ReleaseDecision[] } | { key: string; error: string } | null
  >(null);
  const pairKey = baselineId && candidateId ? `${baselineId}|${candidateId}` : "";

  useEffect(() => {
    listRuns().then(
      (data) => {
        setRuns(data.filter((r) => r.status === "completed"));
        setRunsState("ready");
      },
      (err: unknown) => {
        setRunsError(err instanceof ApiError ? err.message : "Could not load runs.");
        setRunsState("error");
      },
    );
  }, []);

  useEffect(() => {
    if (!pairKey) return;
    Promise.all([getRegression(baselineId, candidateId), listReleaseDecisions(candidateId)]).then(
      ([report, decisions]) => setResult({ key: pairKey, report, decisions }),
      (err: unknown) =>
        setResult({ key: pairKey, error: err instanceof ApiError ? err.message : "Could not compare these runs." }),
    );
  }, [pairKey, baselineId, candidateId]);

  const current = result && result.key === pairKey ? result : null;
  const state: LoadState = !pairKey ? "idle" : !current ? "loading" : "error" in current ? "error" : "ready";
  const report = current && "report" in current ? current.report : null;
  const decisions = current && "decisions" in current ? current.decisions : [];
  const error = current && "error" in current ? current.error : "";

  function select(which: "baseline" | "candidate", id: string) {
    const next = new URLSearchParams(params.toString());
    if (id) next.set(which, id);
    else next.delete(which);
    router.replace(`/regression?${next}`);
  }

  const matching = decisions.filter((d) => d.baseline_run_id === baselineId);

  return (
    <main className="page">
      <h1>Regression</h1>
      <p className="meta-line">
        Compare two completed runs of the same dataset version. Every delta and verdict is computed by the API from
        persisted runs; release decisions come from <code className="mono">agentforge gate</code> and are immutable.
      </p>

      {runsState === "loading" && <div className="state-box">Loading runs…</div>}
      {runsState === "error" && (
        <div className="state-box error">
          <strong>Could not load runs.</strong>
          <div style={{ marginTop: 8 }}>{runsError}</div>
        </div>
      )}
      {runsState === "ready" && runs.length < 2 && (
        <div className="state-box">
          A comparison needs at least two completed runs. Start them on the <Link href="/runs">Runs</Link> page or with{" "}
          <code className="mono">agentforge evaluate</code>.
        </div>
      )}
      {runsState === "ready" && runs.length >= 2 && (
        <form className="run-form" aria-label="Choose runs to compare" onSubmit={(e) => e.preventDefault()}>
          <label className="wide">
            Baseline run
            <select value={baselineId} onChange={(e) => select("baseline", e.target.value)}>
              <option value="">(choose a run)</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {runLabel(r)}
                </option>
              ))}
            </select>
          </label>
          <label className="wide">
            Candidate run
            <select value={candidateId} onChange={(e) => select("candidate", e.target.value)}>
              <option value="">(choose a run)</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {runLabel(r)}
                </option>
              ))}
            </select>
          </label>
        </form>
      )}

      {state === "idle" && runsState === "ready" && runs.length >= 2 && (
        <div className="state-box">Choose a baseline and a candidate run.</div>
      )}
      {state === "loading" && <div className="state-box">Comparing…</div>}
      {state === "error" && (
        <div className="state-box error" role="alert">
          <strong>Can&apos;t compare these runs.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && report && (
        <>
          <h2 className="section-title">Run-level metrics</h2>
          <div className="table-scroll">
            <table aria-label="Run-level deltas">
              <thead>
                <tr>
                  <th>Metric</th>
                  <th>Baseline</th>
                  <th>Candidate</th>
                  <th>Change</th>
                </tr>
              </thead>
              <tbody>
                {RUN_METRICS.map((m) => {
                  const d = report.summary[m.key];
                  return (
                    <tr key={m.key} data-metric={m.key}>
                      <td>
                        {m.label} <span className="meta-line">({m.direction} is better)</span>
                      </td>
                      <td>{formatValue(m.key, d.baseline)}</td>
                      <td>{formatValue(m.key, d.candidate)}</td>
                      <td>
                        <DeltaMarker d={d} direction={m.direction} metric={m.key} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <h2 className="section-title">Per-evaluator</h2>
          <div className="table-scroll">
            <table aria-label="Per-evaluator deltas">
              <thead>
                <tr>
                  <th>Evaluator</th>
                  <th>Mean score</th>
                  <th>Pass rate</th>
                  <th>Mean value</th>
                </tr>
              </thead>
              <tbody>
                {report.metrics.map((m) => (
                  <tr key={m.name} data-evaluator={m.name}>
                    <td>
                      <span className="mono">{m.name}</span>
                      {!m.comparable && (
                        <div className="meta-line">
                          not comparable ({m.baseline_version ?? "absent"} vs {m.candidate_version ?? "absent"})
                        </div>
                      )}
                    </td>
                    <td>
                      <DeltaMarker d={m.mean_score} direction="higher" metric="mean_score" />
                    </td>
                    <td>
                      <DeltaMarker d={m.pass_rate} direction="higher" metric="pass_rate" />
                    </td>
                    <td>
                      <DeltaMarker d={m.mean_value} direction="lower" metric={m.unit === "ms" ? "x_ms" : "mean_value"} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h2 className="section-title">Cases</h2>
          <div className="case-classes">
            {CASE_CLASSES.map((cls) => (
              <CaseList key={cls.key} cls={cls} cases={report.cases[cls.key]} />
            ))}
          </div>

          <h2 className="section-title">Release decision</h2>
          {matching.length > 0 ? (
            matching.map((d) => <Decision key={d.id} decision={d} />)
          ) : (
            <div className="state-box">
              No release decision for this candidate against this baseline yet. Run{" "}
              <code className="mono">
                agentforge gate --candidate {candidateId} --baseline {"<environment or run id>"}
              </code>
              {decisions.length > 0 && ` (${decisions.length} decision(s) exist for this candidate against other baselines).`}
            </div>
          )}
        </>
      )}
    </main>
  );
}

export default function RegressionPage() {
  // useSearchParams needs a Suspense boundary in the app router.
  return (
    <Suspense fallback={<main className="page"><div className="state-box">Loading…</div></main>}>
      <RegressionView />
    </Suspense>
  );
}
