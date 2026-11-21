"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  ["/admin", "Sources"],
  ["/admin/runs", "Runs"],
  ["/admin/audit", "Audit"],
] as const;

export function AdminNav() {
  const path = usePathname();
  // Sources owns /admin and /admin/sources/...; the other tabs own their prefix.
  const current = (href: string) =>
    href === "/admin" ? path === "/admin" || path.startsWith("/admin/sources") : path.startsWith(href);
  return (
    <nav className="subnav" aria-label="Admin">
      {LINKS.map(([href, label]) => (
        <Link key={href} href={href} aria-current={current(href) ? "page" : undefined}>
          {label}
        </Link>
      ))}
    </nav>
  );
}
