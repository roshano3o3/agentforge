"use client";

import { useEffect, useState } from "react";
import { ApiError, createReplay, getReplayOptions } from "@/lib/api";
import { formatOverrideValue as describeValue, formatOverrides } from "@/lib/format";
import type { Replay, ReplayOptions, ReplaySetting } from "@/lib/types";

type Values = Record<string, unknown>;
type Loaded = { options: ReplayOptions } | { error: string };

/** What the control shows: the override if set, else the adapter's default. */
function shown(setting: ReplaySetting, value: unknown): unknown {
  return value !== undefined ? value : setting.default;
}


function SettingControl({
  setting,
  value,
  onChange,
  idPrefix,
}: {
  setting: ReplaySetting;
  value: unknown;
  onChange: (v: unknown) => void;
  idPrefix: string;
}) {
  const id = `${idPrefix}${setting.name}`;
  const overridden = value !== undefined;
  const current = shown(setting, value);
  const reset = overridden && (
    <button type="button" className="link-button" onClick={() => onChange(undefined)}>
      reset
    </button>
  );
  const hint = (
    <span className="meta-line">
      {overridden ? <span className="badge badge-label">override</span> : "default"}
      {setting.default !== null && setting.default !== undefined && <> · adapter default {describeValue(setting.default)}</>}{" "}
      {reset}
    </span>
  );

  if (setting.kind === "object") {
    const nested = (value as Values | undefined) ?? {};
    return (
      <fieldset className="replay-object" data-setting={setting.name}>
        <legend>
          <span className="mono">{setting.name}</span> {setting.description && <>— {setting.description}</>}
        </legend>
        {setting.fields.map((f) => (
          <SettingControl
            key={f.name}
            setting={f}
            idPrefix={`${id}.`}
            value={nested[f.name]}
            onChange={(v) => {
              const next = { ...nested };
              if (v === undefined) delete next[f.name];
              else next[f.name] = v;
              onChange(Object.keys(next).length ? next : undefined);
            }}
          />
        ))}
      </fieldset>
    );
  }

  if (setting.kind === "bool") {
    return (
      <div className="replay-setting" data-setting={setting.name}>
        <label className="checkbox" htmlFor={id}>
          <input id={id} type="checkbox" checked={current === true} onChange={(e) => onChange(e.target.checked)} />{" "}
          <span className="mono">{setting.name}</span> — {setting.description}
        </label>
        {hint}
      </div>
    );
  }

  let control: React.ReactNode;
  if (setting.kind === "choice") {
    control = (
      <select id={id} value={current === null || current === undefined ? "" : String(current)} onChange={(e) => onChange(e.target.value || undefined)}>
        {(setting.default === null || setting.default === undefined) && <option value="">(the adapter&apos;s own)</option>}
        {setting.choices.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
    );
  } else if (setting.kind === "int" || setting.kind === "float") {
    control = (
      <input
        id={id}
        type="number"
        step={setting.kind === "int" ? 1 : "any"}
        min={setting.minimum ?? undefined}
        max={setting.maximum ?? undefined}
        value={current === null || current === undefined ? "" : String(current)}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
      />
    );
  } else if (setting.kind === "text") {
    control = (
      <textarea
        id={id}
        rows={4}
        maxLength={setting.max_length ?? undefined}
        value={typeof current === "string" ? current : ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
      />
    );
  } else {
    control = (
      <input
        id={id}
        type="text"
        maxLength={setting.max_length ?? undefined}
        value={typeof current === "string" ? current : ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
      />
    );
  }
  return (
    <div className="replay-setting" data-setting={setting.name}>
      <label htmlFor={id}>
        <span className="mono">{setting.name}</span> — {setting.description}
        {(setting.minimum !== null || setting.maximum !== null) && (
          <span className="meta-line">
            {" "}
            ({setting.minimum ?? "…"}–{setting.maximum ?? "…"})
          </span>
        )}
      </label>
      {control}
      {hint}
    </div>
  );
}

/** Builds a replay request from the run's recorded replay options. Only the settings you change are
 * sent as overrides; an untouched form replays the case exactly as it ran. */
export default function ReplayForm({ resultId, onCreated }: { resultId: string; onCreated: (r: Replay) => void }) {
  const [loaded, setLoaded] = useState<{ key: string; value: Loaded } | null>(null);
  const [values, setValues] = useState<Values>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    getReplayOptions(resultId).then(
      (options) => setLoaded({ key: resultId, value: { options } }),
      (err: unknown) =>
        setLoaded({
          key: resultId,
          value: { error: err instanceof ApiError ? err.message : "Could not load the replay options." },
        }),
    );
  }, [resultId]);

  const current = loaded && loaded.key === resultId ? loaded.value : null;
  if (!current) return <div className="state-box">Loading replay options…</div>;
  if ("error" in current) {
    return (
      <div className="state-box error" role="alert">
        <strong>Could not load the replay options.</strong>
        <div style={{ marginTop: 8 }}>{current.error}</div>
      </div>
    );
  }
  const { options } = current;
  const replayable = options.adapter_type !== null;
  const overrides = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== undefined));
  const summary = Object.keys(overrides).length
    ? formatOverrides(overrides)
    : "none — replays the case exactly as it ran";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setSubmitError(null);
    try {
      onCreated(await createReplay(resultId, overrides));
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : "Could not queue the replay.");
      setSubmitting(false);
    }
  }

  return (
    <form className="run-form replay-form" onSubmit={submit} aria-label="Replay this case">
      <p className="meta-line wide">
        Re-runs <span className="mono">{options.case_key}</span> with the original run&apos;s adapter (
        <span className="mono">{options.adapter_target}</span>), dataset version and evaluator versions. The worker
        executes it; the original run is never changed.
      </p>
      {!options.overrides_supported && (
        <div className="state-box wide" data-replay-unavailable>
          <strong>Overrides aren&apos;t available for this case.</strong>
          <div style={{ marginTop: 8 }}>{options.reason}</div>
        </div>
      )}
      {options.overrides_supported && (
        <fieldset className="wide">
          <legend>Overrides (declared by the adapter)</legend>
          {options.settings.map((s) => (
            <SettingControl
              key={s.name}
              setting={s}
              idPrefix="replay-"
              value={values[s.name]}
              onChange={(v) => setValues((prev) => ({ ...prev, [s.name]: v }))}
            />
          ))}
          {options.worker_checks && (
            <p className="meta-line">The adapter also checks combinations of these settings when the replay runs.</p>
          )}
        </fieldset>
      )}
      <p className="wide" data-overrides-summary>
        Overrides: <span className="mono">{summary}</span>
      </p>
      {submitError && (
        <div className="form-error wide" role="alert">
          {submitError}
        </div>
      )}
      {replayable && (
        <button type="submit" className="publish-button" disabled={submitting}>
          {submitting ? "Queueing…" : Object.keys(overrides).length ? "Replay with overrides" : "Replay without overrides"}
        </button>
      )}
    </form>
  );
}
