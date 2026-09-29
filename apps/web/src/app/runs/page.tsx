"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, listRuns } from "@/lib/api";
import type { EvaluationRunSummary } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

function formatPct(value: number | null): string {
  return value === null ? "-" : `${(value * 100).toFixed(0)}%`;
}

function formatScore(value: number | null): string {
  return value === null ? "-" : value.toFixed(3);
}

export default function RunsPage() {
  const [runs, setRuns] = useState<EvaluationRunSummary[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    listRuns()
      .then((data) => {
        if (cancelled) return;
        setRuns(data);
        setState("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Unexpected error loading runs.");
        setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="page">
      <h1>Evaluation runs</h1>
      <p className="meta-line">
        Every row below is a real, persisted evaluation run executed by{" "}
        <code className="mono">agentforge evaluate</code>. Nothing on this page is hardcoded.
      </p>

      {state === "loading" && <div className="state-box">Loading runs…</div>}

      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load runs.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && runs.length === 0 && (
        <div className="state-box">
          No evaluation runs yet.
          <div style={{ marginTop: 8 }}>
            Run <code className="mono">agentforge evaluate --app rag-assistant --app-version v1
            --dataset rag-support --dataset-version latest --adapter rag_app.adapter:answer</code>{" "}
            to create one.
          </div>
        </div>
      )}

      {state === "ready" && runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Application</th>
              <th>Dataset</th>
              <th>Evaluator</th>
              <th>Provider</th>
              <th>Status</th>
              <th>Mean score</th>
              <th>Pass rate</th>
              <th>Cases</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id}>
                <td>
                  <Link href={`/runs/${run.id}`} className="mono">
                    {run.id.slice(0, 8)}
                  </Link>
                </td>
                <td>
                  {run.application_name}@{run.application_version}
                </td>
                <td>
                  {run.dataset_name} v{run.dataset_version}
                </td>
                <td className="mono">
                  {run.evaluator_name}@{run.evaluator_version}
                </td>
                <td>{run.provider_type}</td>
                <td>
                  <span
                    className={`badge ${
                      run.status === "completed"
                        ? "badge-pass"
                        : run.status === "failed"
                          ? "badge-fail"
                          : "badge-neutral"
                    }`}
                  >
                    {run.status}
                  </span>
                </td>
                <td>{formatScore(run.mean_score)}</td>
                <td>{formatPct(run.pass_rate)}</td>
                <td>{run.case_count}</td>
                <td>{new Date(run.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
