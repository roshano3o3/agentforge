"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  createDraftVersion,
  getDataset,
  listDatasetVersions,
  newDraftFromVersion,
  patchDraftVersion,
  publishVersion,
  type TestCaseInput,
} from "@/lib/api";
import type { Dataset, DatasetVersion, DraftTestCaseForm, EvaluatorConfig, TestCase } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

const EMPTY_FORM: DraftTestCaseForm = {
  case_key: "",
  input: "",
  expected_answer: "",
  expected_context: "",
  tags: "",
};

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function toTestCaseInput(tc: TestCase): TestCaseInput {
  return {
    case_key: tc.case_key,
    input: tc.input,
    expected_answer: tc.expected_answer,
    expected_context: tc.expected_context,
    tags: tc.tags,
    // Carried through unchanged: a draft PATCH replaces the whole case, so
    // dropping this here would silently erase the case's evaluator config.
    evaluators: tc.evaluators,
  };
}

function formToTestCaseInput(f: DraftTestCaseForm): TestCaseInput {
  return {
    case_key: f.case_key,
    input: f.input,
    expected_answer: f.expected_answer.trim() || null,
    expected_context: splitList(f.expected_context),
    tags: splitList(f.tags),
    evaluators: null, // inherits the version's default evaluators
  };
}

/** One line per configured evaluator: "+name {params}" or "-name" (dropped default). */
function ConfigSummary({ config, emptyText }: { config: EvaluatorConfig | null; emptyText: string }) {
  if (config === null || Object.keys(config).length === 0) return <span className="meta-line">{emptyText}</span>;
  return (
    <div className="config-list">
      {Object.entries(config).map(([key, params]) => (
        <span key={key} className={`mono config-item ${params === false ? "config-off" : ""}`}>
          {params === false
            ? `-${key}`
            : `${key}${params && typeof params === "object" && Object.keys(params).length ? " " + JSON.stringify(params) : ""}`}
        </span>
      ))}
    </div>
  );
}

function DefaultEvaluators({ version }: { version: DatasetVersion }) {
  return (
    <div className="meta-line" style={{ margin: "4px 0 8px" }}>
      Default evaluators:{" "}
      <ConfigSummary
        config={version.default_evaluators}
        emptyText="(none configured: every evaluator the run pins applies)"
      />
    </div>
  );
}

function DraftVersionCard({
  datasetName,
  version,
  onChanged,
  onError,
}: {
  datasetName: string;
  version: DatasetVersion;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [form, setForm] = useState<DraftTestCaseForm>(EMPTY_FORM);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [busy, setBusy] = useState(false);
  const [publishing, setPublishing] = useState(false);

  async function submitReplace(testCases: TestCaseInput[]) {
    setBusy(true);
    try {
      await patchDraftVersion(datasetName, version.version, testCases);
      onChanged();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "Could not update draft.");
    } finally {
      setBusy(false);
    }
  }

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (!form.case_key.trim() || !form.input.trim()) return;
    const next = [...version.test_cases.map(toTestCaseInput), formToTestCaseInput(form)];
    await submitReplace(next);
    setForm(EMPTY_FORM);
  }

  async function handleDelete(caseKey: string) {
    const next = version.test_cases.filter((tc) => tc.case_key !== caseKey).map(toTestCaseInput);
    await submitReplace(next);
  }

  function startEdit(tc: TestCase) {
    setEditingKey(tc.case_key);
    setEditText(tc.input);
  }

  async function saveEdit(caseKey: string) {
    const next = version.test_cases.map((tc) =>
      tc.case_key === caseKey ? { ...toTestCaseInput(tc), input: editText } : toTestCaseInput(tc),
    );
    await submitReplace(next);
    setEditingKey(null);
  }

  async function handlePublish() {
    setPublishing(true);
    try {
      await publishVersion(datasetName, version.version);
      onChanged();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "Could not publish version.");
    } finally {
      setPublishing(false);
    }
  }

  return (
    <div className="version-block">
      <h3>
        v{version.version} <span className="badge badge-neutral">DRAFT</span>{" "}
        <span className="meta-line">· created {new Date(version.created_at).toLocaleString()}</span>
      </h3>
      <DefaultEvaluators version={version} />

      {version.test_cases.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>case_key</th>
              <th>input</th>
              <th>expected_context</th>
              <th>case evaluators</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {version.test_cases.map((tc) => (
              <tr key={tc.id}>
                <td className="mono">{tc.case_key}</td>
                <td>
                  {editingKey === tc.case_key ? (
                    <input type="text" value={editText} onChange={(e) => setEditText(e.target.value)} />
                  ) : (
                    tc.input
                  )}
                </td>
                <td>
                  <div className="doc-id-list">
                    {tc.expected_context.map((id) => (
                      <span key={id} className="doc-id-pill relevant">
                        {id}
                      </span>
                    ))}
                  </div>
                </td>
                <td>
                  <ConfigSummary config={tc.evaluators} emptyText="defaults" />
                </td>
                <td>
                  {editingKey === tc.case_key ? (
                    <>
                      <button type="button" disabled={busy} onClick={() => saveEdit(tc.case_key)}>
                        Save
                      </button>{" "}
                      <button type="button" onClick={() => setEditingKey(null)}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button type="button" disabled={busy} onClick={() => startEdit(tc)}>
                        Edit
                      </button>{" "}
                      <button type="button" disabled={busy} onClick={() => handleDelete(tc.case_key)}>
                        Delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <form onSubmit={handleAdd} className="draft-form">
        <input
          type="text"
          placeholder="case_key (e.g. refund-policy-001)"
          value={form.case_key}
          onChange={(e) => setForm({ ...form, case_key: e.target.value })}
          required
        />
        <input
          type="text"
          placeholder="input (the question)"
          value={form.input}
          onChange={(e) => setForm({ ...form, input: e.target.value })}
          required
        />
        <input
          type="text"
          placeholder="expected_context (comma-separated doc ids)"
          value={form.expected_context}
          onChange={(e) => setForm({ ...form, expected_context: e.target.value })}
        />
        <input
          type="text"
          placeholder="tags (comma-separated)"
          value={form.tags}
          onChange={(e) => setForm({ ...form, tags: e.target.value })}
        />
        <button type="submit" disabled={busy}>
          Add case
        </button>
      </form>

      <button
        type="button"
        onClick={handlePublish}
        disabled={publishing || version.test_cases.length === 0}
        className="publish-button"
        title={version.test_cases.length === 0 ? "Add at least one test case first" : undefined}
      >
        {publishing ? "Publishing…" : "Publish this version"}
      </button>
    </div>
  );
}

function PublishedVersionCard({
  datasetName,
  version,
  onChanged,
  onError,
}: {
  datasetName: string;
  version: DatasetVersion;
  onChanged: () => void;
  onError: (message: string) => void;
}) {
  const [copying, setCopying] = useState(false);

  async function handleNewDraft() {
    setCopying(true);
    try {
      await newDraftFromVersion(datasetName, version.version);
      onChanged();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : "Could not create new version.");
    } finally {
      setCopying(false);
    }
  }

  return (
    <div className="version-block">
      <h3>
        v{version.version} <span className="badge badge-pass">🔒 PUBLISHED</span>{" "}
        <span className="meta-line">
          · published {version.published_at ? new Date(version.published_at).toLocaleString() : ""}
        </span>
      </h3>
      <DefaultEvaluators version={version} />
      <table>
        <thead>
          <tr>
            <th>case_key</th>
            <th>input</th>
            <th>expected_context</th>
            <th>case evaluators</th>
          </tr>
        </thead>
        <tbody>
          {version.test_cases.map((tc) => (
            <tr key={tc.id}>
              <td className="mono">{tc.case_key}</td>
              <td>{tc.input}</td>
              <td>
                <div className="doc-id-list">
                  {tc.expected_context.map((id) => (
                    <span key={id} className="doc-id-pill relevant">
                      {id}
                    </span>
                  ))}
                </div>
              </td>
              <td>
                <ConfigSummary config={tc.evaluators} emptyText="defaults" />
                {(tc.expected_answer_contains.length > 0 || tc.expected_answer_regex) && (
                  <div className="meta-line" title="Written before per-case evaluator config; still applied">
                    legacy:{" "}
                    {tc.expected_answer_contains.length > 0 &&
                      `contains ${JSON.stringify(tc.expected_answer_contains)} `}
                    {tc.expected_answer_regex && `regex ${tc.expected_answer_regex}`}
                  </div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" onClick={handleNewDraft} disabled={copying} className="publish-button">
        {copying ? "Creating…" : "Create new version from this"}
      </button>
    </div>
  );
}

export default function DatasetDetailPage() {
  const params = useParams<{ name: string }>();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [versions, setVersions] = useState<DatasetVersion[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [actionError, setActionError] = useState("");
  const [creatingDraft, setCreatingDraft] = useState(false);

  // State is only set in promise callbacks (never synchronously inside the
  // effect); a reload after an action keeps the current content on screen.
  const load = useCallback(
    () =>
      Promise.all([getDataset(params.name), listDatasetVersions(params.name)]).then(
        ([ds, v]) => {
          setDataset(ds);
          setVersions(v);
          setState("ready");
        },
        (err: unknown) => {
          setError(err instanceof ApiError ? err.message : "Unexpected error loading dataset.");
          setState("error");
        },
      ),
    [params.name],
  );

  useEffect(() => {
    load();
  }, [load]);

  async function handleNewDraft() {
    if (!dataset) return;
    setCreatingDraft(true);
    setActionError("");
    try {
      await createDraftVersion(dataset.id, []);
      load();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not create draft.");
    } finally {
      setCreatingDraft(false);
    }
  }

  return (
    <main className="page">
      <Link href="/datasets" className="back-link">
        ← All datasets
      </Link>

      {state === "loading" && <div className="state-box">Loading…</div>}
      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load dataset &quot;{params.name}&quot;.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && dataset && (
        <>
          <h1>{dataset.name}</h1>
          <p className="meta-line">{dataset.description ?? "(no description)"}</p>
          <p className="meta-line">
            A version is a <strong>draft</strong> (editable — add/edit/delete test cases) until you{" "}
            <strong>publish</strong> it, which freezes it forever. To change a published version&apos;s
            content, use &quot;Create new version from this&quot; to start a new draft copied from it.
          </p>

          <button type="button" onClick={handleNewDraft} disabled={creatingDraft} className="publish-button">
            {creatingDraft ? "Creating…" : "New draft"}
          </button>
          {actionError && <p className="form-error">{actionError}</p>}

          {versions.length === 0 ? (
            <div className="state-box">No versions yet. Click &quot;New draft&quot; to start one.</div>
          ) : (
            versions.map((v) =>
              v.status === "draft" ? (
                <DraftVersionCard
                  key={v.id}
                  datasetName={dataset.name}
                  version={v}
                  onChanged={load}
                  onError={setActionError}
                />
              ) : (
                <PublishedVersionCard
                  key={v.id}
                  datasetName={dataset.name}
                  version={v}
                  onChanged={load}
                  onError={setActionError}
                />
              ),
            )
          )}
        </>
      )}
    </main>
  );
}
