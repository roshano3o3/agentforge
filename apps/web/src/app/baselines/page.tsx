"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, listBaselines } from "@/lib/api";
import { formatPct } from "@/lib/format";
import type { Baseline } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export default function BaselinesPage() {
  const [rows, setRows] = useState<Baseline[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");

  useEffect(() => {
    listBaselines().then(
      (data) => {
        setRows(data);
        setState("ready");
      },
      (err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Unexpected error loading baselines.");
        setState("error");
      },
    );
  }, []);

  return (
    <main className="page">
      <h1>Baselines</h1>
      <p className="meta-line">
        A baseline is a pointer: (application, environment) → one completed run. Setting it changes only the pointer;
        nothing is copied. Set one with <code className="mono">agentforge baseline set &lt;run_id&gt; --env production</code>.
      </p>

      {state === "loading" && <div className="state-box">Loading baselines…</div>}
      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load baselines.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}
      {state === "ready" && rows.length === 0 && (
        <div className="state-box">
          No baselines set yet. Point an environment at a completed run with{" "}
          <code className="mono">agentforge baseline set &lt;run_id&gt; --env production</code>.
        </div>
      )}
      {state === "ready" && rows.length > 0 && (
        <div className="table-scroll">
          <table aria-label="Baselines">
            <thead>
              <tr>
                <th>Application</th>
                <th>Environment</th>
                <th>Run</th>
                <th>App version</th>
                <th>Dataset</th>
                <th>Pass rate</th>
                <th>Set</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((b) => (
                <tr key={`${b.application_id}:${b.environment}`} data-baseline={`${b.application_name}:${b.environment}`}>
                  <td>{b.application_name}</td>
                  <td>
                    <span className="badge badge-label">{b.environment}</span>
                  </td>
                  <td>
                    <Link href={`/runs/${b.run_id}`} className="mono">
                      {b.run_id.slice(0, 8)}
                    </Link>
                  </td>
                  <td>{b.application_version}</td>
                  <td>
                    {b.dataset_name} v{b.dataset_version}
                  </td>
                  <td>{formatPct(b.pass_rate)}</td>
                  <td>{new Date(b.set_at).toLocaleString()}</td>
                  <td>
                    <Link href={`/regression?baseline=${b.run_id}`}>Compare a run against it →</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
