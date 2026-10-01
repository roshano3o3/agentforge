"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/applications", label: "Applications" },
  { href: "/datasets", label: "Datasets" },
  { href: "/runs", label: "Runs" },
  { href: "/regression", label: "Regression" },
  { href: "/baselines", label: "Baselines" },
  { href: "/safety", label: "Safety" },
];

export default function Nav() {
  const pathname = usePathname();

  return (
    <nav className="nav">
      <Link href="/runs" className="nav-title">
        AgentForge
      </Link>
      {LINKS.map((link) => (
        <Link
          key={link.href}
          href={link.href}
          className={`nav-link ${pathname.startsWith(link.href) ? "active" : ""}`}
        >
          {link.label}
        </Link>
      ))}
      <span className="nav-link disabled" title="Built in a later phase">
        Trace Explorer
      </span>
    </nav>
  );
}
