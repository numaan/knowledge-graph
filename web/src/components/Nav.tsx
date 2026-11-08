"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  ["/search", "Search"],
  ["/ask", "Ask"],
  ["/domain", "Domain"],
  ["/requirements", "Requirements"],
] as const;
const ADMIN = ["/admin", "Admin"] as const;

export function Nav({ showAdmin = false }: { showAdmin?: boolean }) {
  const path = usePathname();
  const links = showAdmin ? [...LINKS, ADMIN] : LINKS;
  return (
    <nav className="nav" aria-label="Main">
      {links.map(([href, label]) => (
        <Link key={href} href={href} aria-current={path.startsWith(href) ? "page" : undefined}>
          {label}
        </Link>
      ))}
    </nav>
  );
}
