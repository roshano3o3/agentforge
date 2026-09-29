"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiError, getRun } from "@/lib/api";
import type { EvaluationRun, EvaluationResult } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

function formatScore(value: number | null): string {
  return value === null ? "-" : value.toFixed(3);
}

function ResultRow({ result }: { result: EvaluationResult }) {
  const relevant = new Set(result.expected_context);
  return (
    <tr>
      <td style={{ minWidth: 160 }}>{result.case_key}</td>
      <td style={{ maxWidth: 280 }}>{result.input}</td>
      <td>
        <div className="doc-id-list">
          {result.expected_context.length === 0 && <span className="meta-line">(none)</span>}
          {result.expected_context.map((id) => (
            <span key={id} className="doc-id-pill relevant">
              {id}
            </span>
          ))}
        </div>
      </td>
      <td>
        <div className="doc-id-list">
          {result.retrieved_doc_ids.length === 0 && <span className="meta-line">(none)</span>}
          {result.retrieved_doc_ids.map((id) => (
            <span key={id} className={`doc-id-pill ${relevant.has(id) ? "relevant" : ""}`}>
              {id}
            </span>
          ))}
        </div>
      </td>
      <td>{formatScore(result.score)}</td>
      <td>
        {result.status === "error" ? (
          <span className="badge badge-error">error</span>
        ) : result.passed === null ? (
          <span className="badge badge-neutral">-</span>
        ) : result.passed ? (
          <span className="badge badge-pass">pass</span>
        ) : (
          <span className="badge badge-fail">fail</span>
        )}
      </td>
      <td>{result.latency_ms}</td>
      <td>
        {result.status === "error" ? (
          <span className="meta-line">{result.error_message}</span>
        ) : (
          <details className="evidence">
            <summary>evidence</summary>
            <pre>{JSON.stringify(result.evidence, null, 2)}</pre>
          </details>
        )}
      </td>
    </tr>
  );
}

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setState("loading");
    getRun(params.id)
      .then((data) => {
        if (cancelled) return;
        setRun(data);
        setState("ready");
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Unexpected error loading run.");
        setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [params.id]);

  return (
    <main className="page">
      <Link href="/runs" className="back-link">
        ← All runs
      </Link>

      {state === "loading" && <div className="state-box">Loading run…</div>}

      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load run {params.id}.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && run && (
        <>
          <h1>
            {run.application_name}@{run.application_version}
          </h1>
          <p className="meta-line mono">{run.id}</p>

          <div className="summary-row">
            <div className="summary-stat">
              <div className="label">Status</div>
              <div className="value">{run.status}</div>
            </div>
            <div className="summary-stat">
              <div className="label">Mean score</div>
              <div className="value">{formatScore(run.mean_score)}</div>
            </div>
            <div className="summary-stat">
              <div className="label">Pass rate</div>
              <div className="value">
                {run.pass_rate === null ? "-" : `${(run.pass_rate * 100).toFixed(0)}%`}
              </div>
            </div>
            <div className="summary-stat">
              <div className="label">Cases</div>
              <div className="value">{run.case_count}</div>
            </div>
            <div className="summary-stat">
              <div className="label">Avg latency</div>
              <div className="value">
                {run.avg_latency_ms === null ? "-" : `${run.avg_latency_ms.toFixed(1)}ms`}
              </div>
            </div>
          </div>

          <p className="meta-line">
            Dataset: {run.dataset_name} v{run.dataset_version} &nbsp;·&nbsp; Evaluator:{" "}
            <span className="mono">
              {run.evaluator_name}@{run.evaluator_version}
            </span>{" "}
            &nbsp;·&nbsp; Provider: {run.provider_type} &nbsp;·&nbsp; Environment:{" "}
            {run.environment} &nbsp;·&nbsp; Threshold: {run.threshold} &nbsp;·&nbsp; Commit:{" "}
            <span className="mono">{run.git_commit_sha ?? "(none recorded)"}</span>
          </p>
          <p className="meta-line">
            Created {new Date(run.created_at).toLocaleString()}
            {run.completed_at && ` · Completed ${new Date(run.completed_at).toLocaleString()}`}
          </p>

          <div className="heuristic-note">
            <strong>heuristic_context_precision</strong> is a deterministic document-ID overlap
            proxy (|retrieved ∩ expected relevant| / |retrieved|, 0.0 when nothing was
            retrieved) — not the full RAGAS context-precision metric and not an LLM-judge score.
            See docs/evaluators.md for the exact formula.
          </div>

          <table>
            <thead>
              <tr>
                <th>Case</th>
                <th>Input</th>
                <th>Expected relevant docs</th>
                <th>Retrieved docs</th>
                <th>Score</th>
                <th>Passed</th>
                <th>Latency (ms)</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {run.results.map((result) => (
                <ResultRow key={result.id} result={result} />
              ))}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}
