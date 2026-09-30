import type { RunStatus } from "@/lib/types";
import { FIXTURE_BASED } from "@/lib/types";

const STATUS_CLASS: Record<RunStatus, string> = {
  pending: "badge-neutral",
  running: "badge-running",
  completed: "badge-pass",
  failed: "badge-fail",
};

export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`badge ${STATUS_CLASS[status]}`}>{status}</span>;
}

/** Run/metric labels. "fixture-based" gets an explanation on hover: it must
 * never read as model quality or an LLM judgment. */
export function LabelBadges({ labels }: { labels: string[] }) {
  return (
    <>
      {labels.map((label) => (
        <span
          key={label}
          className="badge badge-label"
          title={
            label === FIXTURE_BASED
              ? "Produced by the local-deterministic provider: a synthetic app scored by deterministic heuristics. Not model quality; not an LLM judgment."
              : label === "estimated"
                ? "Estimated from adapter-reported tokens x rates in config/pricing.yaml"
                : undefined
          }
        >
          {label}
        </span>
      ))}
    </>
  );
}
