"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import NewRunForm from "@/components/NewRunForm";
import { LabelBadges, StatusBadge } from "@/components/RunBadges";
import { ApiError, listRuns } from "@/lib/api";
import { formatEstimatedUsd, formatMs, formatPct, isActive } from "@/lib/format";
import type { EvaluationRunSummary } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";
const POLL_MS = 2000;

export default function RunsPage() {
  const [runs, setRuns] = useState<EvaluationRunSummary[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string>("");
  const [showForm, setShowForm] = useState(false);

  const load = useCallback(
    () =>
      listRuns().then(
        (data) => {
          setRuns(data);
          setState("ready");
        },
        (err: unknown) => {
          setError(err instanceof ApiError ? err.message : "Unexpected error loading runs.");
          setState("error");
        },
      ),
    [],
  );

  useEffect(() => {
    load();
  }, [load]);

  // Keep refreshing while anything is still queued or executing.
  const anyActive = runs.some((r) => isActive(r.status));
  useEffect(() => {
    if (!anyActive) return;
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [anyActive, load]);

  return (
    <main className="page">
      <h1>Evaluation runs</h1>
      <p className="meta-line">
        Every row is a real, persisted run, executed by the AgentForge worker. Nothing on this page is hardcoded.
      </p>

      <button type="button" className="publish-button" onClick={() => setShowForm(!showForm)}>
        {showForm ? "Hide new run form" : "New run"}
      </button>
      {showForm && <NewRunForm />}

      {state === "loading" && <div className="state-box">Loading runs…</div>}

      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load runs.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && runs.length === 0 && (
        <div className="state-box">
          No evaluation runs yet. Start one with &quot;New run&quot; above, or from the CLI:
          <div style={{ marginTop: 8 }}>
            <code className="mono">
              agentforge evaluate --app rag-assistant --app-version v1 --dataset rag-support --adapter
              rag_app.adapter:answer
            </code>
          </div>
        </div>
      )}

      {state === "ready" && runs.length > 0 && (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Application</th>
                <th>Dataset</th>
                <th>Status</th>
                <th>Cases</th>
                <th>Pass rate</th>
                <th>Latency p50 / p95</th>
                <th>Cost</th>
                <th>Labels</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => {
                const agg = run.aggregates;
                return (
                  <tr key={run.id} data-run-id={run.id}>
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
                    <td>
                      <StatusBadge status={run.status} />
                    </td>
                    <td>
                      {run.progress.completed_cases}/{run.progress.total_cases}
                    </td>
                    <td>{formatPct(agg?.pass_rate)}</td>
                    <td>
                      {agg ? `${formatMs(agg.latency_ms.p50)} / ${formatMs(agg.latency_ms.p95)}` : "-"}
                    </td>
                    <td>{formatEstimatedUsd(agg?.estimated_cost_usd?.total)}</td>
                    <td>
                      <LabelBadges labels={run.labels} />
                    </td>
                    <td>{new Date(run.created_at).toLocaleString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
