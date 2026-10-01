"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import Trajectory from "@/components/Trajectory";
import { ApiError, getRegression, getRun, listRuns } from "@/lib/api";
import { formatPct } from "@/lib/format";
import type { EvaluationResult, EvaluationRun, EvaluationRunSummary, MetricDelta } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

// The seven categories `agentforge adversarial generate` produces, in a fixed order.
const CATEGORIES = [
  "injection_direct",
  "injection_indirect",
  "poisoned_context",
  "malformed_tool_args",
  "tool_failure",
  "pii_probe",
  "unauthorized_tool",
];

function hasSafety(r: EvaluationRunSummary): boolean {
  return r.status === "completed" && !!r.aggregates?.safety;
}

function runLabel(r: EvaluationRunSummary): string {
  const s = r.aggregates?.safety;
  const passed = s ? Object.values(s.by_category).reduce((n, c) => n + c.passed, 0) : 0;
  return (
    `${r.application_name}@${r.application_version} · ${r.dataset_name} v${r.dataset_version} · ` +
    `${s ? `${passed}/${s.cases}` : "-"} · ${r.id.slice(0, 8)} · ${new Date(r.created_at).toLocaleString()}`
  );
}

/** Pass-rate change in points, colored better/worse; "= 0" when unchanged. */
function Change({ d }: { d: MetricDelta | undefined }) {
  if (!d || d.delta === null) return <span className="meta-line">n/a</span>;
  if (Math.abs(d.delta) < 1e-12) return <span className="delta delta-same">= 0</span>;
  const up = d.delta > 0;
  return (
    <span className={`delta ${up ? "delta-better" : "delta-worse"}`} data-direction={up ? "better" : "worse"}>
      <span aria-label={up ? "up" : "down"}>{up ? "▲" : "▼"}</span> {up ? "+" : "-"}
      {(Math.abs(d.delta) * 100).toFixed(1)} pts
    </span>
  );
}

function FailingCase({ result }: { result: EvaluationResult }) {
  const failed = result.metrics.filter((m) => m.passed === false);
  return (
    <details className="safety-case" data-case-key={result.case_key}>
      <summary>
        <span className="mono">{result.case_key}</span>
        <span className="meta-line"> — failed: {failed.map((m) => m.evaluator_name).join(", ") || result.status}</span>
      </summary>
      <div className="safety-case-body">
        <p className="meta-line">
          Technique <span className="mono">{result.safety?.technique ?? "-"}</span> · derived from{" "}
          <span className="mono">{result.safety?.source_case ?? "-"}</span>
        </p>
        <p>
          <strong>Input:</strong> {result.input}
        </p>
        <p>
          <strong>Answer:</strong> {result.output_answer ?? <em>(none: {result.status})</em>}
        </p>
        <ul className="safety-reasons">
          {failed.map((m) => (
            <li key={m.evaluator_name} data-evaluator={m.evaluator_name}>
              <span className="mono">{m.evaluator_name}</span>: {m.reason}
            </li>
          ))}
        </ul>
        {result.steps.length > 0 && <Trajectory result={result} defaultOpen />}
      </div>
    </details>
  );
}

function SafetyView() {
  const router = useRouter();
  const params = useSearchParams();
  const runId = params.get("run") ?? "";
  const baselineId = params.get("baseline") ?? "";
  const category = params.get("category") ?? "";

  const [runs, setRuns] = useState<EvaluationRunSummary[]>([]);
  const [runsState, setRunsState] = useState<LoadState>("loading");
  const [runsError, setRunsError] = useState("");
  // The detail for one (run, baseline) selection; its loading state is derived from the key.
  const key = runId ? `${runId}|${baselineId}` : "";
  const [loaded, setLoaded] = useState<
    | { key: string; run: EvaluationRun; deltas: Record<string, MetricDelta> | null; compareError: string }
    | { key: string; error: string }
    | null
  >(null);

  useEffect(() => {
    listRuns().then(
      (data) => {
        setRuns(data.filter(hasSafety));
        setRunsState("ready");
      },
      (err: unknown) => {
        setRunsError(err instanceof ApiError ? err.message : "Could not load runs.");
        setRunsState("error");
      },
    );
  }, []);

  useEffect(() => {
    if (!key) return;
    const compare = baselineId
      ? getRegression(baselineId, runId).then(
          (report) => ({ deltas: report.safety, compareError: "" }),
          (err: unknown) => ({
            deltas: null,
            compareError: err instanceof ApiError ? err.message : "Could not compare with the baseline.",
          }),
        )
      : Promise.resolve({ deltas: null, compareError: "" });
    Promise.all([getRun(runId), compare]).then(
      ([run, c]) => setLoaded({ key, run, ...c }),
      (err: unknown) => setLoaded({ key, error: err instanceof ApiError ? err.message : "Could not load the run." }),
    );
  }, [key, runId, baselineId]);

  const current = loaded && loaded.key === key ? loaded : null;
  const run = current && "run" in current ? current.run : null;
  const error = current && "error" in current ? current.error : "";

  function set(name: "run" | "baseline" | "category", value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(name, value);
    else next.delete(name);
    if (name !== "category") next.delete("category");
    router.replace(`/safety?${next}`);
  }

  const byCategory = run?.aggregates?.safety?.by_category ?? {};
  const categories = [
    ...CATEGORIES.filter((c) => c in byCategory),
    ...Object.keys(byCategory).filter((c) => !CATEGORIES.includes(c)),
  ];
  const failing = (run?.results ?? []).filter((r) => r.safety?.category === category && !r.passed);
  const deltas = current && "deltas" in current ? current.deltas : null;
  const compareError = current && "compareError" in current ? current.compareError : "";

  return (
    <main className="page">
      <h1>Safety</h1>
      <p className="meta-line">
        Case pass rate per attack category for runs of an adversarial dataset (generated with{" "}
        <code className="mono">agentforge adversarial generate</code>). Every number is stored with the run; the change
        column comes from the API&apos;s regression report. Results of the example agent are fixture-based.
      </p>

      {runsState === "loading" && <div className="state-box">Loading runs…</div>}
      {runsState === "error" && (
        <div className="state-box error" role="alert">
          <strong>Could not load runs.</strong>
          <div style={{ marginTop: 8 }}>{runsError}</div>
        </div>
      )}
      {runsState === "ready" && runs.length === 0 && (
        <div className="state-box" data-empty="no-safety-runs">
          No completed run of an adversarial dataset yet. Generate one with{" "}
          <code className="mono">agentforge adversarial generate</code>, publish it, and evaluate it (or start a run on
          the <Link href="/runs">Runs</Link> page).
        </div>
      )}
      {runsState === "ready" && runs.length > 0 && (
        <form className="run-form" aria-label="Choose a safety run" onSubmit={(e) => e.preventDefault()}>
          <label className="wide">
            Run
            <select value={runId} onChange={(e) => set("run", e.target.value)}>
              <option value="">(choose a run)</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {runLabel(r)}
                </option>
              ))}
            </select>
          </label>
          <label className="wide">
            Compare with baseline (optional)
            <select value={baselineId} onChange={(e) => set("baseline", e.target.value)}>
              <option value="">(none)</option>
              {runs
                .filter((r) => r.id !== runId)
                .map((r) => (
                  <option key={r.id} value={r.id}>
                    {runLabel(r)}
                  </option>
                ))}
            </select>
          </label>
        </form>
      )}

      {runsState === "ready" && runs.length > 0 && !runId && <div className="state-box">Choose a run.</div>}
      {key && !current && <div className="state-box">Loading the run…</div>}
      {error && (
        <div className="state-box error" role="alert">
          <strong>Can&apos;t show this run.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {run && categories.length === 0 && (
        <div className="state-box" data-empty="no-categories">
          This run&apos;s dataset has no adversarial cases, so there&apos;s nothing to break down by attack category.
        </div>
      )}

      {run && categories.length > 0 && (
        <>
          {compareError && (
            <div className="state-box error" role="alert">
              <strong>Can&apos;t compare with that baseline.</strong>
              <div style={{ marginTop: 8 }}>{compareError}</div>
            </div>
          )}
          <h2 className="section-title">
            {run.application_name}@{run.application_version} · {run.dataset_name} v{run.dataset_version}
            {run.labels.map((l) => (
              <span key={l} className="badge badge-label">
                {l}
              </span>
            ))}
          </h2>
          <div className="table-scroll">
            <table aria-label="Pass rate per attack category">
              <thead>
                <tr>
                  <th>Attack category</th>
                  {deltas && <th>Baseline</th>}
                  <th>{deltas ? "Candidate" : "Pass rate"}</th>
                  {deltas && <th>Change</th>}
                  <th>Failing cases</th>
                </tr>
              </thead>
              <tbody>
                {categories.map((c) => {
                  const entry = byCategory[c];
                  const d = deltas?.[c];
                  const fails = entry.cases - entry.passed;
                  return (
                    <tr key={c} data-category={c} className={c === category ? "row-selected" : ""}>
                      <td className="mono">{c}</td>
                      {deltas && <td>{d?.baseline === null || !d ? "-" : formatPct(d.baseline)}</td>}
                      <td>
                        {formatPct(entry.pass_rate)}{" "}
                        <span className="meta-line">
                          ({entry.passed}/{entry.cases})
                        </span>
                      </td>
                      {deltas && (
                        <td>
                          <Change d={d} />
                        </td>
                      )}
                      <td>
                        {fails === 0 ? (
                          <span className="meta-line">none</span>
                        ) : (
                          <button
                            type="button"
                            className="link-button"
                            aria-pressed={c === category}
                            onClick={() => set("category", c === category ? "" : c)}
                          >
                            {fails} failing {c === category ? "▾" : "▸"}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {category && (
            <section aria-label={`Failing cases: ${category}`}>
              <h2 className="section-title">
                Failing cases: <span className="mono">{category}</span>
              </h2>
              {failing.length === 0 ? (
                <div className="state-box">No failing case in this category.</div>
              ) : (
                <>
                  <p className="meta-line">
                    Each case shows the evaluators that failed it, with their reasons, and the trajectory with the
                    steps they blamed highlighted.
                  </p>
                  {failing.map((r) => (
                    <FailingCase key={r.id} result={r} />
                  ))}
                </>
              )}
            </section>
          )}
          <p className="meta-line">
            <Link href={`/runs/${run.id}`}>Open the full run</Link>
            {run.dataset_content_hash && (
              <>
                {" "}
                · dataset content <span className="mono">{run.dataset_content_hash.slice(0, 19)}…</span>
              </>
            )}
          </p>
        </>
      )}
    </main>
  );
}

export default function SafetyPage() {
  // useSearchParams needs a Suspense boundary in the app router.
  return (
    <Suspense fallback={<main className="page"><div className="state-box">Loading…</div></main>}>
      <SafetyView />
    </Suspense>
  );
}
