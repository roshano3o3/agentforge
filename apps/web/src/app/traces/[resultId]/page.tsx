"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { Fragment, useEffect, useMemo, useState } from "react";
import ReplayForm from "@/components/ReplayForm";
import ReplayList from "@/components/ReplayList";
import { ApiError, getTrace } from "@/lib/api";
import type { Trace, TraceSpan } from "@/lib/types";

type Loaded = { key: string; trace: Trace } | { key: string; error: string; status?: number };

// What the server's PII redaction leaves behind (agentforge_api.tracing).
const PLACEHOLDER = /\[(EMAIL|PHONE|SSN|CARD|ACCOUNT)\]/g;

interface Row {
  span: TraceSpan;
  depth: number;
}

interface Failure {
  evaluator: string;
  reason: string;
  steps: number[];
  spanId: string;
}

function flatten(spans: TraceSpan[], depth = 0, out: Row[] = []): Row[] {
  for (const span of spans) {
    out.push({ span, depth });
    flatten(span.children, depth + 1, out);
  }
  return out;
}

function parseSteps(value: unknown): number[] {
  if (Array.isArray(value)) return value.filter((n): n is number => typeof n === "number");
  if (typeof value === "string") {
    try {
      return parseSteps(JSON.parse(value));
    } catch {
      return [];
    }
  }
  return [];
}

/** Failed evaluators, from the stored `evaluate <name>` spans (their reasons are redacted like everything else). */
function failures(rows: Row[]): Failure[] {
  return rows
    .filter((r) => r.span.name.startsWith("evaluate ") && r.span.attributes["agentforge.evaluator.passed"] === false)
    .map((r) => ({
      evaluator: String(r.span.attributes["agentforge.evaluator.name"] ?? r.span.name.slice(9)),
      reason: String(r.span.attributes["agentforge.evaluator.reason"] ?? ""),
      steps: parseSteps(r.span.attributes["agentforge.evaluator.failing_steps"]),
      spanId: r.span.span_id,
    }));
}

/** Text with the redaction placeholders marked, so it's obvious what was removed. */
function Redacted({ text }: { text: string }) {
  const parts: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(PLACEHOLDER)) {
    parts.push(text.slice(last, m.index));
    parts.push(
      <mark key={m.index} className="redacted" title="PII removed before this span was stored">
        {m[0]}
      </mark>,
    );
    last = (m.index ?? 0) + m[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

function pretty(value: unknown): string {
  if (typeof value !== "string") return JSON.stringify(value, null, 2);
  const t = value.trim();
  if ((t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"))) {
    try {
      return JSON.stringify(JSON.parse(t), null, 2);
    } catch {
      return value;
    }
  }
  return value;
}

function SpanDetails({ span }: { span: TraceSpan }) {
  const keys = Object.keys(span.attributes).sort();
  return (
    <div className="span-details">
      <div className="meta-line">
        span <span className="mono">{span.span_id}</span> · {span.kind} · {span.service} · status {span.status_code}
        {span.step_index !== null && <> · reported for step {span.step_index}</>}
      </div>
      {span.status_message && (
        <div className="span-error-message" role="note">
          <strong>Error:</strong> <Redacted text={span.status_message} />
        </div>
      )}
      <table className="span-attributes" aria-label={`Attributes of ${span.name}`}>
        <tbody>
          {keys.map((k) => (
            <tr key={k}>
              <td className="mono">{k}</td>
              <td>
                <pre className="step-value">
                  <Redacted text={pretty(span.attributes[k])} />
                </pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {span.events.length > 0 && (
        <div>
          <div className="step-field-label">Events</div>
          {span.events.map((e, i) => (
            <pre key={i} className="step-value">
              {e.name} @ {new Date(e.time).toISOString()}
              {"\n"}
              <Redacted text={pretty(JSON.stringify(e.attributes))} />
            </pre>
          ))}
        </div>
      )}
    </div>
  );
}

function Waterfall({ rows, blamed }: { rows: Row[]; blamed: Map<string, Failure[]> }) {
  const [open, setOpen] = useState<Set<string>>(() => new Set([...blamed.keys()]));
  const start = Math.min(...rows.map((r) => Date.parse(r.span.start_time)));
  const end = Math.max(...rows.map((r) => Date.parse(r.span.end_time)));
  const total = Math.max(end - start, 1);
  function toggle(id: string) {
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  }
  return (
    <div className="table-scroll">
      <table className="waterfall" aria-label="Span waterfall">
        <thead>
          <tr>
            <th>Span</th>
            <th>Duration</th>
            <th className="waterfall-bar-col">Timeline ({total} ms)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ span, depth }) => {
            const flags = blamed.get(span.span_id) ?? [];
            const error = span.status_code === "ERROR";
            const failing = flags.length > 0;
            const redacted = typeof span.attributes["agentforge.redaction.count"] === "number";
            const left = ((Date.parse(span.start_time) - start) / total) * 100;
            const width = Math.max((Date.parse(span.end_time) - Date.parse(span.start_time)) / total * 100, 0.4);
            const isOpen = open.has(span.span_id);
            return (
              <Fragment key={span.span_id}>
                <tr
                  className={`waterfall-row${failing ? " span-failing" : ""}${error ? " span-error" : ""}`}
                  data-span-name={span.name}
                  data-failing={failing}
                  data-error={error}
                >
                  <td style={{ paddingLeft: 8 + depth * 18 }}>
                    <button
                      type="button"
                      className="link-button span-toggle"
                      aria-expanded={isOpen}
                      onClick={() => toggle(span.span_id)}
                    >
                      {isOpen ? "▾" : "▸"} <span className="mono">{span.name}</span>
                    </button>
                    {span.step_index !== null && <span className="badge badge-label">step {span.step_index}</span>}
                    {error && <span className="badge badge-fail">error</span>}
                    {redacted && (
                      <span className="badge badge-redacted" title={String(span.attributes["agentforge.redacted"])}>
                        redacted
                      </span>
                    )}
                    {flags.map((f) => (
                      <div key={f.evaluator} className="span-flag" data-evaluator={f.evaluator}>
                        <span className="mono">{f.evaluator}</span>: <Redacted text={f.reason} />
                      </div>
                    ))}
                  </td>
                  <td className="mono">{span.duration_ms.toFixed(3)} ms</td>
                  <td className="waterfall-bar-col">
                    <div className="waterfall-track">
                      <div className="waterfall-bar" style={{ left: `${left}%`, width: `${width}%` }} />
                    </div>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="waterfall-details">
                    <td colSpan={3}>
                      <SpanDetails span={span} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export default function TraceExplorerPage() {
  const params = useParams<{ resultId: string }>();
  const resultId = params.resultId;
  const router = useRouter();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);

  useEffect(() => {
    getTrace(resultId).then(
      (trace) => setLoaded({ key: resultId, trace }),
      (err: unknown) =>
        setLoaded({
          key: resultId,
          error: err instanceof ApiError ? err.message : "Could not load the trace.",
          status: err instanceof ApiError ? err.status : undefined,
        }),
    );
  }, [resultId]);

  const current = loaded && loaded.key === resultId ? loaded : null;
  const trace = current && "trace" in current ? current.trace : null;
  const rows = useMemo(() => (trace ? flatten(trace.spans) : []), [trace]);
  const failed = useMemo(() => failures(rows), [rows]);
  // A failed evaluator blames the spans of the steps it names, and its own evaluate span.
  const blamed = useMemo(() => {
    const map = new Map<string, Failure[]>();
    const add = (id: string, f: Failure) => map.set(id, [...(map.get(id) ?? []), f]);
    for (const f of failed) {
      add(f.spanId, f);
      for (const r of rows) if (r.span.step_index !== null && f.steps.includes(r.span.step_index)) add(r.span.span_id, f);
    }
    return map;
  }, [failed, rows]);
  const redactions = rows.reduce((n, r) => n + (Number(r.span.attributes["agentforge.redaction.count"]) || 0), 0);

  return (
    <main className="page">
      <div className="trace-header">
        <h1>Trace</h1>
        <button
          type="button"
          className="publish-button"
          aria-expanded={replayOpen}
          onClick={() => setReplayOpen(!replayOpen)}
          title="Re-run this case, optionally with settings changed"
        >
          Replay
        </button>
      </div>
      {replayOpen && (
        <section className="replay-panel" aria-label="Replay">
          <ReplayForm resultId={resultId} onCreated={(r) => router.push(`/replays/${r.id}`)} />
        </section>
      )}

      {!current && <div className="state-box">Loading the trace…</div>}
      {current && "error" in current && current.status === 404 && (
        <div className="state-box" data-empty="no-trace">
          <strong>No trace for this case.</strong>
          <div style={{ marginTop: 8 }}>{current.error}</div>
        </div>
      )}
      {current && "error" in current && current.status !== 404 && (
        <div className="state-box error" role="alert">
          <strong>Could not load the trace.</strong>
          <div style={{ marginTop: 8 }}>{current.error}</div>
        </div>
      )}

      {trace && (
        <>
          <p className="meta-line">
            Case <span className="mono">{trace.case_key}</span> · run{" "}
            <Link href={`/runs/${trace.run_id}`}>{trace.run_id.slice(0, 8)}</Link> · trace{" "}
            <span className="mono">{trace.trace_id}</span> · {trace.span_count} spans
          </p>
          <p className="meta-line">
            Spans as the API, the worker and the agent recorded them. Personal data was replaced before storage
            {redactions > 0 ? (
              <>
                {" "}
                ({redactions} value{redactions === 1 ? "" : "s"} in this trace, marked{" "}
                <mark className="redacted">[EMAIL]</mark>, <mark className="redacted">[ACCOUNT]</mark>, …)
              </>
            ) : (
              " (none in this trace)"
            )}
            . Results of the example agent are fixture-based.
          </p>
          {failed.length > 0 ? (
            <section className="state-box error" aria-label="Failed evaluators">
              <strong>
                {failed.length} evaluator{failed.length === 1 ? "" : "s"} failed this case
              </strong>
              <ul className="safety-reasons">
                {failed.map((f) => (
                  <li key={f.evaluator} data-evaluator={f.evaluator}>
                    <span className="mono">{f.evaluator}</span>
                    {f.steps.length > 0 && <> (step {f.steps.join(", ")})</>}: <Redacted text={f.reason} />
                  </li>
                ))}
              </ul>
            </section>
          ) : (
            <div className="state-box">No evaluator failed this case.</div>
          )}
          <h2 className="section-title">Spans</h2>
          <Waterfall rows={rows} blamed={blamed} />
          <ReplayList resultId={resultId} />
        </>
      )}
    </main>
  );
}
