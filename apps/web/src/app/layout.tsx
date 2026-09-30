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
          Local dev build — no authentication, localhost only, not for public deployment. Example data is
          synthetic; all evaluators are deterministic (no LLM judge).
        </div>
        {children}
      </body>
    </html>
  );
}
