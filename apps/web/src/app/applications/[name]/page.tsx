"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ApiError, createApplicationVersion, getApplication, listApplicationVersions } from "@/lib/api";
import type { Application, ApplicationVersion } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export default function ApplicationDetailPage() {
  const params = useParams<{ name: string }>();
  const [application, setApplication] = useState<Application | null>(null);
  const [versions, setVersions] = useState<ApplicationVersion[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [version, setVersion] = useState("");
  const [description, setDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // State is only set in promise callbacks (never synchronously inside the
  // effect); a reload after creating keeps the current content on screen.
  const load = useCallback(
    () =>
      getApplication(params.name)
        .then(async (app) => [app, await listApplicationVersions(app.id)] as const)
        .then(
          ([app, v]) => {
            setApplication(app);
            setVersions(v);
            setState("ready");
          },
          (err: unknown) => {
            setError(err instanceof ApiError ? err.message : "Unexpected error loading application.");
            setState("error");
          },
        ),
    [params.name],
  );

  useEffect(() => {
    load();
  }, [load]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!application || !version.trim()) return;
    setCreating(true);
    setCreateError("");
    try {
      await createApplicationVersion(application.id, version.trim(), description.trim() || null);
      setVersion("");
      setDescription("");
      load();
    } catch (err) {
      setCreateError(err instanceof ApiError ? err.message : "Could not create version.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="page">
      <Link href="/applications" className="back-link">
        ← All applications
      </Link>

      {state === "loading" && <div className="state-box">Loading…</div>}
      {state === "error" && (
        <div className="state-box error">
          <strong>Could not load application &quot;{params.name}&quot;.</strong>
          <div style={{ marginTop: 8 }}>{error}</div>
        </div>
      )}

      {state === "ready" && application && (
        <>
          <h1>{application.name}</h1>
          <p className="meta-line">{application.description ?? "(no description)"}</p>

          <h2>Versions</h2>
          <form onSubmit={handleCreate} className="inline-form">
            <input
              type="text"
              placeholder="Version label (e.g. v1, candidate)"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              required
            />
            <input
              type="text"
              placeholder="Description (optional)"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <button type="submit" disabled={creating}>
              {creating ? "Creating…" : "Create version"}
            </button>
          </form>
          {createError && <p className="form-error">{createError}</p>}

          {versions.length === 0 ? (
            <div className="state-box">No versions yet. Create one above.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Description</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id}>
                    <td className="mono">{v.version}</td>
                    <td>{v.description ?? <span className="meta-line">(none)</span>}</td>
                    <td>{new Date(v.created_at).toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </main>
  );
}
