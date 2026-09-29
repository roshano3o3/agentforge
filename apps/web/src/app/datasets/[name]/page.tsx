"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState, useEffect } from "react";
import {
  ApiError,
  attemptEditPublishedVersion,
  getDataset,
  listDatasetVersions,
  publishDatasetVersion,
} from "@/lib/api";
import type { Dataset, DatasetVersion, DraftTestCase, TestCase } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

const EMPTY_DRAFT: DraftTestCase = {
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

export default function DatasetDetailPage() {
  const params = useParams<{ name: string }>();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [versions, setVersions] = useState<DatasetVersion[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");

  // Draft composer: test cases being built up client-side, not yet
  // published. This is the only place "editing" a test case makes sense --
  // once published, a version is immutable (see the demo panel below).
  const [draft, setDraft] = useState<DraftTestCase[]>([]);
  const [form, setForm] = useState<DraftTestCase>(EMPTY_DRAFT);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");

  // Immutability demo: attempt to PATCH a published version's test case and
  // show the API's real rejection, not a simulated one.
  const [editAttempt, setEditAttempt] = useState<{ status: number; detail: string } | null>(null);
  const [editingCase, setEditingCase] = useState<TestCase | null>(null);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const [editText, setEditText] = useState("");
  const [attemptingEdit, setAttemptingEdit] = useState(false);

  function load() {
    setState("loading");
    getDataset(params.name)
      .then(async (ds) => {
        setDataset(ds);
        const v = await listDatasetVersions(params.name);
        setVersions(v);
        setState("ready");
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Unexpected error loading dataset.");
        setState("error");
      });
  }

  useEffect(load, [params.name]);

  function addToDraft(e: React.FormEvent) {
    e.preventDefault();
    if (!form.case_key.trim() || !form.input.trim()) return;
    setDraft((d) => [...d, form]);
    setForm(EMPTY_DRAFT);
  }

  function removeFromDraft(index: number) {
    setDraft((d) => d.filter((_, i) => i !== index));
  }

  async function handlePublish() {
    if (!dataset || draft.length === 0) return;
    setPublishing(true);
    setPublishError("");
    try {
      await publishDatasetVersion(
        dataset.id,
        draft.map((d) => ({
          case_key: d.case_key,
          input: d.input,
          expected_answer: d.expected_answer.trim() || null,
          expected_context: splitList(d.expected_context),
          tags: splitList(d.tags),
        })),
      );
      setDraft([]);
      load();
    } catch (err) {
      setPublishError(err instanceof ApiError ? err.message : "Could not publish version.");
    } finally {
      setPublishing(false);
    }
  }

  function openEditAttempt(testCase: TestCase, version: number) {
    setEditingCase(testCase);
    setEditingVersion(version);
    setEditText(testCase.input);
    setEditAttempt(null);
  }

  async function submitEditAttempt() {
    if (!editingCase || editingVersion === null || !dataset) return;
    setAttemptingEdit(true);
    try {
      await attemptEditPublishedVersion(dataset.name, editingVersion, { ...editingCase, input: editText });
      // Should never reach here -- the API always rejects this with 409.
      setEditAttempt({ status: 200, detail: "Unexpectedly succeeded — this should never happen." });
    } catch (err) {
      if (err instanceof ApiError) {
        setEditAttempt({ status: err.status ?? 0, detail: err.message });
      } else {
        setEditAttempt({ status: 0, detail: "Unexpected error." });
      }
    } finally {
      setAttemptingEdit(false);
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

          <h2>Compose a new version</h2>
          <p className="meta-line">
            Add test cases below, then publish. Publishing always creates a brand-new, immutable
            version — it never modifies an existing one.
          </p>

          <form onSubmit={addToDraft} className="draft-form">
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
              placeholder="expected_answer (optional)"
              value={form.expected_answer}
              onChange={(e) => setForm({ ...form, expected_answer: e.target.value })}
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
            <button type="submit">Add to draft</button>
          </form>

          {draft.length > 0 && (
            <>
              <table>
                <thead>
                  <tr>
                    <th>case_key</th>
                    <th>input</th>
                    <th>expected_context</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {draft.map((d, i) => (
                    <tr key={i}>
                      <td className="mono">{d.case_key}</td>
                      <td>{d.input}</td>
                      <td>{d.expected_context || <span className="meta-line">(none)</span>}</td>
                      <td>
                        <button type="button" onClick={() => removeFromDraft(i)}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button type="button" onClick={handlePublish} disabled={publishing} className="publish-button">
                {publishing ? "Publishing…" : `Publish version ${(versions[0]?.version ?? 0) + 1}`}
              </button>
            </>
          )}
          {publishError && <p className="form-error">{publishError}</p>}

          <h2>Published versions</h2>
          {versions.length === 0 ? (
            <div className="state-box">No published versions yet.</div>
          ) : (
            versions.map((v) => (
              <div key={v.id} className="version-block">
                <h3>
                  v{v.version}{" "}
                  <span className="meta-line">
                    · published {new Date(v.published_at).toLocaleString()} · immutable
                  </span>
                </h3>
                <table>
                  <thead>
                    <tr>
                      <th>case_key</th>
                      <th>input</th>
                      <th>expected_context</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {v.test_cases.map((tc) => (
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
                          <button type="button" onClick={() => openEditAttempt(tc, v.version)}>
                            Try to edit
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))
          )}

          {editingCase && editingVersion !== null && (
            <div className="edit-attempt-panel">
              <h3>
                Attempting to edit &quot;{editingCase.case_key}&quot; in published v{editingVersion}
              </h3>
              <input type="text" value={editText} onChange={(e) => setEditText(e.target.value)} />
              <button type="button" onClick={submitEditAttempt} disabled={attemptingEdit}>
                {attemptingEdit ? "Sending PATCH…" : "Send PATCH to API"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setEditingCase(null);
                  setEditAttempt(null);
                }}
              >
                Close
              </button>
              {editAttempt && (
                <div className={editAttempt.status === 409 ? "state-box error" : "state-box"}>
                  <strong>HTTP {editAttempt.status}</strong>
                  <div style={{ marginTop: 6 }}>{editAttempt.detail}</div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </main>
  );
}
