import type { Metadata, Viewport } from "next";
import Link from "next/link";
import { auth, oidcEnabled } from "@/auth";
import { Nav } from "@/components/Nav";
import { getMe } from "@/lib/me";
import { ThemeToggle } from "@/components/ThemeToggle";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Knowledge base", template: "%s · Knowledge base" },
  description: "Search and explore the knowledge graph of code, tickets, documents and requirements.",
};
// OIDC_ISSUER is read at run time (container env), so pages are rendered per request.
export const dynamic = "force-dynamic";
export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// Sets the theme before the first paint; the toggle stores the choice in localStorage.
const themeScript = `try{var t=localStorage.getItem("kg-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = oidcEnabled ? await auth() : null;
  const showAdmin = (await getMe(2000)).state === "admin";
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <a className="skip" href="#main">Skip to content</a>
        <header className="header">
          <Link href="/" className="brand">Knowledge base</Link>
          <Nav showAdmin={showAdmin} />
          <form action="/search" role="search" className="header-search">
            <label htmlFor="q-header" className="skip">Search</label>
            <input id="q-header" type="search" name="q" placeholder="Search code, tickets, docs…" />
          </form>
          <ThemeToggle />
          {oidcEnabled && session?.user && (
            <span className="small muted">{session.user.name ?? session.user.email} · <a href="/api/auth/signout">Sign out</a></span>
          )}
        </header>
        <main id="main" tabIndex={-1}>{children}</main>
      </body>
    </html>
  );
}
