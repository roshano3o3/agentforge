"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, createDataset, listDatasets } from "@/lib/api";
import type { Dataset } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export default function DatasetsPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  function load() {
    setState("loading");
    listDatasets()
      .then((data) => {
        setDatasets(data);
        setState("ready");
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Unexpected error loading datasets.");
        setState("error");
      });
  }

  useEffect(load, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    setCreateError("");
    try {
      await createDataset(name.trim(), description.trim() || null);
      setName("");
      setDescription("");
      load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Could not create dataset.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="page">
      <h1>Datasets</h1>
      <p className="meta-line">
        Creating a dataset here makes an empty container with zero versions. Open it to add test
        cases and publish version 1 — publishing always creates a new, immutable version.
      </p>

      <form onSubmit={handleCreate} className="inline-form">
        <input
          type="text"
          placeholder="Dataset name (e.g. rag-support)"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
        <input
          type="text"
          placeholder="Description (optional)"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <button type="submit" disabled={creating}>
          {creating ? "Creating…" : "Create dataset"}
        </button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {state === "loading" && <div className="state-box">Loading datasets…</div>}

      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load datasets.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && datasets.length === 0 && (
        <div className="state-box">No datasets yet. Create one above.</div>
      )}

      {state === "ready" && datasets.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Description</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((ds) => (
              <tr key={ds.id}>
                <td>
                  <Link href={`/datasets/${encodeURIComponent(ds.name)}`}>{ds.name}</Link>
                </td>
                <td>{ds.description ?? <span className="meta-line">(none)</span>}</td>
                <td>{new Date(ds.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
