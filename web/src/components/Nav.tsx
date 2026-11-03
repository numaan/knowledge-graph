"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  ["/search", "Search"],
  ["/ask", "Ask"],
  ["/domain", "Domain"],
  ["/requirements", "Requirements"],
] as const;

export function Nav() {
  const path = usePathname();
  return (
    <nav className="nav" aria-label="Main">
      {LINKS.map(([href, label]) => (
        <Link key={href} href={href} aria-current={path.startsWith(href) ? "page" : undefined}>
          {label}
        </Link>
      ))}
    </nav>
  );
}
