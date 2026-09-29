"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ApiError, createApplication, listApplications } from "@/lib/api";
import type { Application } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export default function ApplicationsPage() {
  const [applications, setApplications] = useState<Application[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  function load() {
    setState("loading");
    listApplications()
      .then((data) => {
        setApplications(data);
        setState("ready");
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Unexpected error loading applications.");
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
      await createApplication(name.trim(), description.trim() || null);
      setName("");
      setDescription("");
      load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Could not create application.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="page">
      <h1>Applications</h1>
      <p className="meta-line">
        The top-level container this system evaluates against (what earlier planning called
        &quot;Project&quot; -- there is no separate Project entity, Application is it).
      </p>

      <form onSubmit={handleCreate} className="inline-form">
        <input
          type="text"
          placeholder="Application name (e.g. rag-assistant)"
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
          {creating ? "Creating…" : "Create application"}
        </button>
      </form>
      {createError && <p className="form-error">{createError}</p>}

      {state === "loading" && <div className="state-box">Loading applications…</div>}

      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load applications.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && applications.length === 0 && (
        <div className="state-box">No applications yet. Create one above.</div>
      )}

      {state === "ready" && applications.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Description</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {applications.map((app) => (
              <tr key={app.id}>
                <td>
                  <Link href={`/applications/${encodeURIComponent(app.name)}`}>{app.name}</Link>
                </td>
                <td>{app.description ?? <span className="meta-line">(none)</span>}</td>
                <td>{new Date(app.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </main>
  );
}
