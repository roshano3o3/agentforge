import type { RunStatus } from "./types";

export function formatPct(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `${(value * 100).toFixed(0)}%`;
}

export function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : value.toFixed(3);
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  return value < 10 ? `${value.toFixed(2)} ms` : `${value.toFixed(0)} ms`;
}

/** Always shown with an "est." prefix: cost is an estimate from configured rates. */
export function formatEstimatedUsd(value: number | null | undefined): string {
  return value === null || value === undefined ? "-" : `est. $${value.toFixed(6)}`;
}

export function formatValue(value: number | null, unit: string | null): string {
  if (value === null) return "-";
  if (unit === "ms") return formatMs(value);
  if (unit === "usd") return formatEstimatedUsd(value);
  return `${Number.isInteger(value) ? value : value.toFixed(2)}${unit ? ` ${unit}` : ""}`;
}

export function isActive(status: RunStatus): boolean {
  return status === "pending" || status === "running";
}

/** A replay override value as the form, the replay page and the list show it (booleans as on/off). */
export function formatOverrideValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "on" : "off";
  if (typeof value === "string") return value.length > 40 ? `${value.slice(0, 39)}…` : value;
  return JSON.stringify(value);
}

export function formatOverrides(overrides: Record<string, unknown>): string {
  const entries = Object.entries(overrides);
  return entries.length ? entries.map(([k, v]) => `${k}=${formatOverrideValue(v)}`).join(", ") : "none";
}
