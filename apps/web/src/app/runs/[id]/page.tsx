"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { LabelBadges, StatusBadge } from "@/components/RunBadges";
import { ApiError, getRun } from "@/lib/api";
import { formatEstimatedUsd, formatMs, formatPct, formatScore, formatValue, isActive } from "@/lib/format";
import type { EvaluationResult, EvaluationRun, MetricScore } from "@/lib/types";
import { FIXTURE_BASED } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";
const POLL_MS = 1500;

function MetricChip({ metric }: { metric: MetricScore }) {
  const verdict = metric.passed === null ? "na" : metric.passed ? "pass" : "fail";
  const shown =
    metric.score !== null ? metric.score.toFixed(2) : metric.value !== null ? formatValue(metric.value, metric.unit) : "n/a";
  return (
    <div className={`metric-chip metric-${verdict}`}>
      <div>
        <span className="mono">{metric.evaluator_name}</span>
        <span className="meta-line"> @{metric.evaluator_version}</span> <strong>{shown}</strong>{" "}
        {metric.passed !== null && <span className="verdict">{metric.passed ? "pass" : "fail"}</span>}
        {metric.labels
          .filter((l) => l !== FIXTURE_BASED)
          .map((l) => (
            <span key={l} className="badge badge-label">
              {l}
            </span>
          ))}
      </div>
      <div className="metric-reason">{metric.reason}</div>
      <details className="evidence">
        <summary>evidence</summary>
        <pre>{JSON.stringify(metric.evidence, null, 2)}</pre>
      </details>
    </div>
  );
}

function ResultRow({ result }: { result: EvaluationResult }) {
  return (
    <tr data-case-key={result.case_key}>
      <td style={{ minWidth: 150 }}>
        <div className="mono">{result.case_key}</div>
        <div className="meta-line">{result.input}</div>
      </td>
      <td style={{ maxWidth: 280 }}>
        {result.status === "ok" ? (
          <>
            <div>{result.output_answer}</div>
            <div className="doc-id-list" style={{ marginTop: 6 }}>
              {result.retrieved_doc_ids.map((id) => (
                <span
                  key={id}
                  className={`doc-id-pill ${result.expected_context.includes(id) ? "relevant" : ""}`}
                  title={result.citations.includes(id) ? "retrieved and cited" : "retrieved"}
                >
                  {id}
                  {result.citations.includes(id) ? " †" : ""}
                </span>
              ))}
            </div>
          </>
        ) : (
          <details className="evidence" open>
            <summary className="error-summary">
              {result.error_type}: {(result.error_message ?? "").split("\n")[0]}
            </summary>
            <pre>{result.error_message}</pre>
          </details>
        )}
      </td>
      <td>
        {result.status === "ok" ? (
          result.passed ? (
            <span className="badge badge-pass">pass</span>
          ) : (
            <span className="badge badge-fail">fail</span>
          )
        ) : (
          <span className="badge badge-error">{result.status}</span>
        )}
        <div className="meta-line" style={{ marginTop: 4 }}>
          {formatMs(result.latency_ms)}
        </div>
      </td>
      <td style={{ minWidth: 320 }}>
        {result.metrics.length === 0 ? (
          <span className="meta-line">not scored ({result.status})</span>
        ) : (
          result.metrics.map((m) => <MetricChip key={`${m.evaluator_name}@${m.evaluator_version}`} metric={m} />)
        )}
      </td>
    </tr>
  );
}

function Summary({ run }: { run: EvaluationRun }) {
  const agg = run.aggregates;
  if (!agg) return null;
  return (
    <>
      <div className="summary-row">
        <div className="summary-stat">
          <div className="label">Pass rate</div>
          <div className="value">{formatPct(agg.pass_rate)}</div>
          <div className="meta-line">
            {agg.passed_count} of {agg.case_count} cases
          </div>
        </div>
        <div className="summary-stat">
          <div className="label">Outcomes</div>
          <div className="value">
            {agg.ok_count} ok · {agg.error_count} error · {agg.timeout_count} timeout
          </div>
        </div>
        <div className="summary-stat">
          <div className="label">Latency p50 / p95</div>
          <div className="value">
            {formatMs(agg.latency_ms.p50)} / {formatMs(agg.latency_ms.p95)}
          </div>
          <div className="meta-line">{agg.latency_ms.basis}</div>
        </div>
        {agg.estimated_cost_usd && (
          <div className="summary-stat">
            <div className="label">Estimated cost</div>
            <div className="value">{formatEstimatedUsd(agg.estimated_cost_usd.total)}</div>
            <div className="meta-line">
              estimated · {agg.estimated_cost_usd.cases_without_estimate} case(s) without an estimate
            </div>
          </div>
        )}
      </div>

      <h2 className="section-title">Per-metric means</h2>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Evaluator</th>
              <th>Mean score</th>
              <th>Mean value</th>
              <th>Pass rate</th>
              <th>Not applicable</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(agg.metrics).map(([key, m]) => (
              <tr key={key}>
                <td className="mono">{key}</td>
                <td>{formatScore(m.mean_score)}</td>
                <td>{formatValue(m.mean_value, m.unit)}</td>
                <td>{formatPct(m.pass_rate)}</td>
                <td>{m.not_applicable_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export default function RunDetailPage() {
  const params = useParams<{ id: string }>();
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string>("");

  const load = useCallback(
    () =>
      getRun(params.id).then(
        (data) => {
          setRun(data);
          setState("ready");
        },
        (err: unknown) => {
          setError(err instanceof ApiError ? err.message : "Unexpected error loading run.");
          setState("error");
        },
      ),
    [params.id],
  );

  useEffect(() => {
    load();
  }, [load]);

  const active = run !== null && isActive(run.status);
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [active, load]);

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
            {run.application_name}@{run.application_version} <StatusBadge status={run.status} />{" "}
            <LabelBadges labels={run.labels} />
          </h1>
          <p className="meta-line mono">{run.id}</p>
          <p className="meta-line">
            Dataset: {run.dataset_name} v{run.dataset_version} · Adapter:{" "}
            <span className="mono">
              {run.adapter_type ? `${run.adapter_type}:${run.adapter_target}` : "(Phase 1 client-side run)"}
            </span>{" "}
            · Provider: {run.provider_type} · Threshold: {run.threshold}
            {run.timeout_seconds !== null && ` · Timeout: ${run.timeout_seconds}s/case`}
            {run.max_latency_ms !== null && ` · Latency budget: ${run.max_latency_ms} ms`} · Commit:{" "}
            <span className="mono">{run.git_commit_sha?.slice(0, 12) ?? "(none recorded)"}</span>
          </p>
          <p className="meta-line">
            Created {new Date(run.created_at).toLocaleString()}
            {run.started_at && ` · Started ${new Date(run.started_at).toLocaleString()}`}
            {run.completed_at && ` · Finished ${new Date(run.completed_at).toLocaleString()}`}
          </p>

          {run.labels.includes(FIXTURE_BASED) && (
            <div className="heuristic-note">
              <strong>Fixture-based results.</strong> This run used the local-deterministic provider: a synthetic
              app scored by deterministic heuristics (string, regex, and document-ID overlap checks). These numbers
              are not model quality, and none of them is an LLM judgment.
            </div>
          )}

          {run.status === "pending" && (
            <div className="state-box" role="status">
              Queued — waiting for a worker to pick it up. This page refreshes automatically.
            </div>
          )}
          {run.status === "running" && (
            <div className="state-box" role="status">
              Running — {run.progress.completed_cases} of {run.progress.total_cases} case(s) done.
              <progress
                value={run.progress.completed_cases}
                max={Math.max(run.progress.total_cases, 1)}
                style={{ display: "block", margin: "10px auto 0", width: "60%" }}
              />
            </div>
          )}
          {run.status === "failed" && (
            <div className="state-box error" role="alert">
              <strong>Run failed.</strong>
              <div style={{ marginTop: 8 }}>{run.error_message ?? "No reason recorded."}</div>
            </div>
          )}

          <Summary run={run} />

          <h2 className="section-title">Per-case results</h2>
          {run.results.length === 0 ? (
            <div className="state-box">
              {isActive(run.status) ? "No cases finished yet." : "This run has no results."}
            </div>
          ) : (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Case</th>
                    <th>Answer / error</th>
                    <th>Outcome</th>
                    <th>Evaluators (score, verdict, reason)</th>
                  </tr>
                </thead>
                <tbody>
                  {run.results.map((result) => (
                    <ResultRow key={result.id} result={result} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="meta-line" style={{ marginTop: 8 }}>
            Doc pills: green = expected-relevant, † = cited in the answer.
          </p>
        </>
      )}
    </main>
  );
}
