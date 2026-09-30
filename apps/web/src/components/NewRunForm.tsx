"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import {
  ApiError,
  createRun,
  listApplications,
  listApplicationVersions,
  listDatasetVersions,
  listDatasets,
  listEvaluators,
} from "@/lib/api";
import type { Application, ApplicationVersion, Dataset, DatasetVersion, Evaluator } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

/** Starts a run: the API creates it `pending` and queues it; the worker
 * (Docker) executes it. Navigates to the run's page, which shows progress. */
export default function NewRunForm() {
  const router = useRouter();
  const [state, setState] = useState<LoadState>("loading");
  const [loadError, setLoadError] = useState("");
  const [apps, setApps] = useState<Application[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [evaluators, setEvaluators] = useState<Evaluator[]>([]);
  const [appVersions, setAppVersions] = useState<ApplicationVersion[]>([]);
  const [datasetVersions, setDatasetVersions] = useState<DatasetVersion[]>([]);

  const [appId, setAppId] = useState("");
  const [appVersionId, setAppVersionId] = useState("");
  const [datasetName, setDatasetName] = useState("");
  const [datasetVersionId, setDatasetVersionId] = useState("");
  const [adapterType, setAdapterType] = useState<"python" | "http">("python");
  const [adapterTarget, setAdapterTarget] = useState("rag_app.adapter:answer");
  const [selected, setSelected] = useState<string[]>([]);
  const [timeoutSeconds, setTimeoutSeconds] = useState("10");
  const [threshold, setThreshold] = useState("0.7");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  useEffect(() => {
    Promise.all([listApplications(), listDatasets(), listEvaluators()])
      .then(([a, d, e]) => {
        setApps(a);
        setDatasets(d);
        setEvaluators(e);
        setSelected(e.map((ev) => ev.key));
        if (a.length) setAppId(a[0].id);
        if (d.length) setDatasetName(d[0].name);
        setState("ready");
      })
      .catch((err: unknown) => {
        setLoadError(err instanceof ApiError ? err.message : "Could not load form options.");
        setState("error");
      });
  }, []);

  useEffect(() => {
    if (!appId) return;
    listApplicationVersions(appId)
      .then((v) => {
        setAppVersions(v);
        setAppVersionId(v[0]?.id ?? "");
      })
      .catch(() => setAppVersions([]));
  }, [appId]);

  useEffect(() => {
    if (!datasetName) return;
    listDatasetVersions(datasetName)
      .then((v) => {
        const published = v.filter((dv) => dv.status === "published");
        setDatasetVersions(published);
        setDatasetVersionId(published[0]?.id ?? "");
      })
      .catch(() => setDatasetVersions([]));
  }, [datasetName]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setSubmitError("");
    try {
      const run = await createRun({
        application_id: appId,
        application_version_id: appVersionId,
        dataset_version_id: datasetVersionId,
        adapter: { type: adapterType, target: adapterTarget.trim() },
        evaluators: selected,
        threshold: Number(threshold),
        timeout_seconds: Number(timeoutSeconds),
        provider_type: "local-deterministic",
      });
      router.push(`/runs/${run.id}`);
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : "Could not start run.");
      setSubmitting(false);
    }
  }

  if (state === "loading") return <div className="state-box">Loading run options…</div>;
  if (state === "error")
    return (
      <div className="state-box error">
        <strong>Could not load run options.</strong>
        <div style={{ marginTop: 8 }}>{loadError}</div>
      </div>
    );
  if (apps.length === 0 || datasets.length === 0)
    return (
      <div className="state-box">
        To start a run you need at least one application (with a version) and one dataset with a published
        version. Create them on the Applications and Datasets pages, or with{" "}
        <code className="mono">agentforge dataset publish datasets/rag_support_v1.yaml</code>.
      </div>
    );

  const canSubmit = appVersionId && datasetVersionId && adapterTarget.trim() && selected.length > 0 && !submitting;

  return (
    <form className="run-form" onSubmit={handleSubmit} aria-label="Start a run">
      <label>
        Application
        <select value={appId} onChange={(e) => setAppId(e.target.value)}>
          {apps.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        App version
        <select value={appVersionId} onChange={(e) => setAppVersionId(e.target.value)}>
          {appVersions.length === 0 && <option value="">(no versions)</option>}
          {appVersions.map((v) => (
            <option key={v.id} value={v.id}>
              {v.version}
            </option>
          ))}
        </select>
      </label>
      <label>
        Dataset
        <select value={datasetName} onChange={(e) => setDatasetName(e.target.value)}>
          {datasets.map((d) => (
            <option key={d.id} value={d.name}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        Published version
        <select value={datasetVersionId} onChange={(e) => setDatasetVersionId(e.target.value)}>
          {datasetVersions.length === 0 && <option value="">(none published)</option>}
          {datasetVersions.map((v) => (
            <option key={v.id} value={v.id}>
              v{v.version} ({v.test_cases.length} cases)
            </option>
          ))}
        </select>
      </label>
      <label>
        Adapter
        <select value={adapterType} onChange={(e) => setAdapterType(e.target.value as "python" | "http")}>
          <option value="python">python (module:function)</option>
          <option value="http">http (URL)</option>
        </select>
      </label>
      <label className="wide">
        Adapter target
        <input
          type="text"
          value={adapterTarget}
          onChange={(e) => setAdapterTarget(e.target.value)}
          placeholder={adapterType === "python" ? "rag_app.adapter:answer" : "http://host.docker.internal:8100/answer"}
        />
      </label>
      <label>
        Per-case timeout (s)
        <input type="number" min="0.1" max="300" step="0.1" value={timeoutSeconds} onChange={(e) => setTimeoutSeconds(e.target.value)} />
      </label>
      <label>
        Pass threshold
        <input type="number" min="0" max="1" step="0.05" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
      </label>
      <fieldset className="wide">
        <legend>Evaluators (all deterministic; none is an LLM judge)</legend>
        {evaluators.map((ev) => (
          <label key={ev.key} className="checkbox" title={ev.description}>
            <input
              type="checkbox"
              checked={selected.includes(ev.key)}
              onChange={(e) =>
                setSelected(e.target.checked ? [...selected, ev.key] : selected.filter((k) => k !== ev.key))
              }
            />
            <span className="mono">{ev.key}</span>
          </label>
        ))}
      </fieldset>
      <p className="meta-line wide">
        Provider: <strong>local-deterministic</strong> — results are labeled fixture-based. The adapter runs in the
        worker container as trusted local code.
      </p>
      <div className="wide">
        <button type="submit" className="publish-button" disabled={!canSubmit}>
          {submitting ? "Starting…" : "Start run"}
        </button>
        {submitError && <p className="form-error" style={{ marginTop: 8 }}>{submitError}</p>}
      </div>
    </form>
  );
}
