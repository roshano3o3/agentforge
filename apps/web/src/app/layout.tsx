import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentForge",
  description: "Evaluation, safety testing, and release gating for AI agents.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <nav className="nav">
          <Link href="/runs" className="nav-title">
            AgentForge
          </Link>
          <Link href="/runs" className="nav-link active">
            Runs
          </Link>
          <span className="nav-link disabled" title="Built in a later phase">
            Regression
          </span>
          <span className="nav-link disabled" title="Built in a later phase">
            Trace Explorer
          </span>
          <span className="nav-link disabled" title="Built in a later phase">
            Safety
          </span>
        </nav>
        <div className="env-banner">
          Phase 1 local dev build — synthetic data, heuristic evaluator only, not for public
          deployment.
        </div>
        {children}
      </body>
    </html>
  );
}
