import { NextResponse, type NextRequest } from "next/server";
import { auth, oidcEnabled } from "@/auth";
import { REFRESH_ERROR } from "@/lib/token-refresh";

// With OIDC_ISSUER set, every page and API call needs a session; without it the app is open (local mode).
// A session whose access token could no longer be refreshed counts as signed out.
const signedIn = auth((req) => {
  const expired = req.auth?.error === REFRESH_ERROR;
  if (req.auth && !expired) return NextResponse.next();
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return NextResponse.json({ error: expired ? "session expired, sign in again" : "unauthorized" }, { status: 401 });
  }
  const url = new URL("/api/auth/signin", req.nextUrl.origin);
  url.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(url);
});

export function proxy(req: NextRequest) {
  if (!oidcEnabled) return NextResponse.next();
  return (signedIn as unknown as (r: NextRequest) => Promise<Response>)(req);
}

export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico|healthz).*)"],
};
