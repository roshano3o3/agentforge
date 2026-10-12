"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import ReplayList from "@/components/ReplayList";
import { ApiError, getReplay } from "@/lib/api";
import { formatMs, formatOverrides, formatValue } from "@/lib/format";
import type { EvaluatorChange, MetricBrief, NumberChange, Replay, ReplayDiff, StepBrief, StepChange } from "@/lib/types";
import { FIXTURE_BASED } from "@/lib/types";

const POLL_MS = 500;
type Loaded = { key: string; replay: Replay } | { key: string; error: string; status?: number };

function Verdict({ passed, data }: { passed: boolean | null; data: string }) {
  if (passed === null) return <span className="badge badge-neutral" data-verdict={data}>-</span>;
  return (
    <span className={`badge ${passed ? "badge-pass" : "badge-fail"} verdict-big`} data-verdict={data}>
      {passed ? "PASS" : "FAIL"}
    </span>
  );
}

function metricText(m: MetricBrief | null): string {
  if (!m) return "-";
  const shown = m.score !== null ? m.score.toFixed(2) : m.value !== null ? formatValue(m.value, m.unit) : "";
  const verdict = m.passed === null ? (shown ? "" : "n/a") : m.passed ? "PASS" : "FAIL";
  return [verdict, shown].filter(Boolean).join(" ");
}

const CHANGE_CLASS: Record<EvaluatorChange["change"], string> = {
  unchanged: "",
  fixed: "change-fixed",
  broken: "change-broken",
  changed: "change-changed",
  added: "change-changed",
  removed: "change-changed",
};

function notApplicable(ev: EvaluatorChange): boolean {
  return (
    ev.change === "unchanged" &&
    [ev.before, ev.after].every((m) => m !== null && m.passed === null && m.score === null && m.value === null)
  );
}

function Evaluators({ diff }: { diff: ReplayDiff }) {
  const na = diff.evaluators.filter(notApplicable).map((e) => e.evaluator_name);
  const rows = diff.evaluators.filter((e) => !notApplicable(e));
  return (
    <>
      <div className="table-scroll">
        <table aria-label="Evaluators before and after" className="replay-evaluators">
          <thead>
            <tr>
              <th>Evaluator</th>
              <th>Before</th>
              <th>After</th>
              <th>Change</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ev) => (
              <tr key={ev.evaluator_name} data-evaluator={ev.evaluator_name} data-change={ev.change} className={CHANGE_CLASS[ev.change]}>
                <td className="mono">{ev.evaluator_name}</td>
                <td>{metricText(ev.before)}</td>
                <td>{metricText(ev.after)}</td>
                <td>
                  <strong>{ev.change}</strong>
                </td>
                <td>{ev.change === "unchanged" ? "" : (ev.after ?? ev.before)?.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {na.length > 0 && (
        <p className="meta-line">
          Not applicable to this case, before and after: <span className="mono">{na.join(", ")}</span>
        </p>
      )}
    </>
  );
}

function stepLabel(s: StepBrief): string {
  if (s.kind === "tool_call") return `${s.name} ${JSON.stringify(s.args)}`;
  if (s.kind === "retrieval") return `retrieval ${JSON.stringify(s.retrieved_doc_ids)}`;
  return "final_answer";
}

function StepCell({ step, change }: { step: StepBrief | null; change: StepChange }) {
  if (!step) return <td className="step-empty" />;
  return (
    <td>
      <span className="step-number-inline">{step.step_index}</span> <span className="mono">{stepLabel(step)}</span>
      {change.op === "changed" &&
        change.changed_fields.map((f) => (
          <pre key={f} className="step-value">
            {f}: {JSON.stringify(step[f as keyof StepBrief])}
          </pre>
        ))}
    </td>
  );
}

function TrajectoryDiff({ steps }: { steps: StepChange[] }) {
  if (steps.length === 0) return <div className="state-box">Neither side reported any steps.</div>;
  return (
    <div className="table-scroll">
      <table aria-label="Trajectory before and after" className="replay-trajectory">
        <thead>
          <tr>
            <th>Original</th>
            <th>Change</th>
            <th>Replay</th>
          </tr>
        </thead>
        <tbody>
          {steps.map((s, i) => (
            <tr key={i} className={`step-${s.op}`} data-op={s.op} data-step-name={s.name || s.kind}>
              <StepCell step={s.before} change={s} />
              <td>
                <strong>{s.op}</strong>
                {s.changed_fields.length > 0 && <div className="meta-line">{s.changed_fields.join(", ")}</div>}
              </td>
              <StepCell step={s.after} change={s} />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AnswerDiff({ diff }: { diff: ReplayDiff }) {
  const { answer } = diff;
  if (!answer.changed) {
    return (
      <div className="answer-diff" data-answer-changed="false">
        <div className="meta-line">Unchanged</div>
        <div>{answer.after ?? "(no answer)"}</div>
      </div>
    );
  }
  return (
    <div className="answer-diff side-by-side" data-answer-changed="true">
      <div>
        <div className="meta-line">Original</div>
        {answer.segments
          .filter((s) => s.op !== "insert")
          .map((s, i) => (s.op === "delete" ? <del key={i}>{s.text}</del> : <span key={i}>{s.text}</span>))}
      </div>
      <div>
        <div className="meta-line">Replay</div>
        {answer.segments
          .filter((s) => s.op !== "delete")
          .map((s, i) => (s.op === "insert" ? <ins key={i}>{s.text}</ins> : <span key={i}>{s.text}</span>))}
      </div>
    </div>
  );
}

function numberRow(label: string, n: NumberChange, format: (v: number) => string) {
  const known = n.before !== null || n.after !== null;
  return (
    <tr>
      <td>{label}</td>
      <td>{n.before === null ? "-" : format(n.before)}</td>
      <td>{n.after === null ? "-" : format(n.after)}</td>
      <td>{!known ? "not reported" : n.delta === null ? "-" : `${n.delta > 0 ? "+" : ""}${format(n.delta)}`}</td>
    </tr>
  );
}

function DiffView({ replay, diff }: { replay: Replay; diff: ReplayDiff }) {
  return (
    <>
      <section className="replay-verdict" aria-label="Case verdict">
        <Verdict passed={diff.before_passed} data="before" /> <span className="verdict-arrow">→</span>{" "}
        <Verdict passed={diff.after_passed} data="after" />
        <span className="meta-line">
          {" "}
          status {diff.before_status} → {diff.after_status} ·{" "}
          {diff.identical ? (
            <strong data-identical="true">identical to the original</strong>
          ) : (
            <span data-identical="false">differs in {diff.differences.length} place(s)</span>
          )}
        </span>
      </section>
      <h2 className="section-title">Evaluators</h2>
      <Evaluators diff={diff} />
      <h2 className="section-title">Trajectory</h2>
      <TrajectoryDiff steps={diff.trajectory} />
      <h2 className="section-title">Final answer</h2>
      <AnswerDiff diff={diff} />
      <h2 className="section-title">Latency and tokens</h2>
      <div className="table-scroll">
        <table aria-label="Latency and tokens">
          <thead>
            <tr>
              <th />
              <th>Original</th>
              <th>Replay</th>
              <th>Change</th>
            </tr>
          </thead>
          <tbody>
            {numberRow("Latency", diff.latency_ms, formatMs)}
            {numberRow("Input tokens", diff.input_tokens, String)}
            {numberRow("Output tokens", diff.output_tokens, String)}
          </tbody>
        </table>
      </div>
      {replay.labels.includes(FIXTURE_BASED) && (
        <p className="meta-line">
          fixture-based: the local-deterministic provider (synthetic agent, deterministic evaluators). Not model
          quality, and not an LLM judgment.
        </p>
      )}
    </>
  );
}

export default function ReplayPage() {
  const params = useParams<{ id: string }>();
  const replayId = params.id;
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      getReplay(replayId).then(
        (replay) => {
          if (stopped) return;
          setLoaded({ key: replayId, replay });
          if (replay.status === "pending" || replay.status === "running") timer = setTimeout(load, POLL_MS);
        },
        (err: unknown) => {
          if (stopped) return;
          setLoaded({
            key: replayId,
            error: err instanceof ApiError ? err.message : "Could not load the replay.",
            status: err instanceof ApiError ? err.status : undefined,
          });
        },
      );
    load();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [replayId]);

  const current = loaded && loaded.key === replayId ? loaded : null;
  const replay = current && "replay" in current ? current.replay : null;

  return (
    <main className="page">
      <h1>Replay</h1>
      {!current && <div className="state-box">Loading the replay…</div>}
      {current && "error" in current && (
        <div className="state-box error" role="alert" data-empty={current.status === 404 ? "no-replay" : undefined}>
          <strong>{current.status === 404 ? "No such replay." : "Could not load the replay."}</strong>
          <div style={{ marginTop: 8 }}>{current.error}</div>
        </div>
      )}
      {replay && (
        <>
          <p className="meta-line">
            Case <span className="mono">{replay.case_key}</span> ·{" "}
            <Link href={`/traces/${replay.original_result_id}`}>original trace</Link> · run{" "}
            <Link href={`/runs/${replay.original_run_id}`}>{replay.original_run_id.slice(0, 8)}</Link> · adapter{" "}
            <span className="mono">{replay.adapter_target}</span>
          </p>
          <p data-overrides>
            Overrides:{" "}
            <span className="mono">{formatOverrides(replay.overrides)}</span>
          </p>
          {replay.provenance.warnings.map((w) => (
            <div key={w} className="state-box warning" role="status" data-provenance-warning>
              <strong>Warning:</strong> {w}
            </div>
          ))}
          {replay.provenance.replay.code_sha256 && (
            <p className="meta-line" data-code-version>
              Ran code {replay.provenance.replay.code_version} (source {replay.provenance.replay.code_sha256.slice(0, 12)}
              ){replay.provenance.same_code === true && " — the same as the original run"}.
            </p>
          )}
          {(replay.status === "pending" || replay.status === "running") && (
            <div className="state-box" role="status" data-replay-status={replay.status}>
              {replay.status === "pending" ? "Queued — waiting for the worker…" : "Running the case…"}
            </div>
          )}
          {replay.status === "failed" && (
            <div className="state-box error" role="alert" data-replay-status="failed">
              <strong>The replay failed.</strong>
              <div style={{ marginTop: 8 }}>{replay.error_message}</div>
            </div>
          )}
          {replay.status === "completed" && replay.diff && <DiffView replay={replay} diff={replay.diff} />}
          <ReplayList resultId={replay.original_result_id} highlight={replay.id} refresh={replay.status === "completed" ? 1 : 0} />
        </>
      )}
    </main>
  );
}
