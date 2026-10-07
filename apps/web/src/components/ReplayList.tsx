"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, listReplays } from "@/lib/api";
import { formatOverrides } from "@/lib/format";
import type { ReplaySummary } from "@/lib/types";

function verdict(passed: boolean | null): string {
  return passed === null ? "-" : passed ? "PASS" : "FAIL";
}

/** Every replay of one case result, newest first. `highlight` marks the one being viewed. */
export default function ReplayList({ resultId, highlight, refresh = 0 }: { resultId: string; highlight?: string; refresh?: number }) {
  const [loaded, setLoaded] = useState<{ key: string; replays?: ReplaySummary[]; error?: string } | null>(null);
  const key = `${resultId}:${refresh}`;

  useEffect(() => {
    listReplays(resultId).then(
      (replays) => setLoaded({ key, replays }),
      (err: unknown) => setLoaded({ key, error: err instanceof ApiError ? err.message : "Could not load replays." }),
    );
  }, [resultId, key]);

  const current = loaded && loaded.key === key ? loaded : null;
  return (
    <section aria-label="Previous replays">
      <h2 className="section-title">Replays of this case</h2>
      {!current && <div className="state-box">Loading replays…</div>}
      {current?.error && (
        <div className="state-box error" role="alert">
          <strong>Could not load the replays.</strong>
          <div style={{ marginTop: 8 }}>{current.error}</div>
        </div>
      )}
      {current?.replays && current.replays.length === 0 && (
        <div className="state-box" data-empty="no-replays">
          This case hasn&apos;t been replayed yet.
        </div>
      )}
      {current?.replays && current.replays.length > 0 && (
        <div className="table-scroll">
          <table aria-label="Replays">
            <thead>
              <tr>
                <th>Replay</th>
                <th>Status</th>
                <th>Overrides</th>
                <th>Verdict</th>
                <th>Same as original?</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {current.replays.map((r) => (
                <tr key={r.id} data-replay-id={r.id} className={r.id === highlight ? "replay-current" : undefined}>
                  <td>
                    <Link href={`/replays/${r.id}`} className="mono">
                      {r.id.slice(0, 8)}
                    </Link>
                    {r.id === highlight && <span className="badge badge-label">viewing</span>}
                  </td>
                  <td>{r.status}</td>
                  <td className="mono">{formatOverrides(r.overrides)}</td>
                  <td>
                    {verdict(r.original_passed)} → {verdict(r.passed)}
                  </td>
                  <td>{r.identical === null ? "-" : r.identical ? "identical" : "differs"}</td>
                  <td>{new Date(r.created_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
