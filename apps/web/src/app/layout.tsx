import type { Metadata } from "next";
import Nav from "@/components/Nav";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentForge",
  description: "Evaluation, safety testing, and release gating for AI agents.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <Nav />
        <div className="env-banner">
          Phase 1 local dev build — synthetic data, heuristic evaluator only, not for public
          deployment.
        </div>
        {children}
      </body>
    </html>
  );
}
