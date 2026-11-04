import NextAuth from "next-auth";
import { getToken } from "next-auth/jwt";
import Keycloak from "next-auth/providers/keycloak";
import { headers } from "next/headers";
import { needsRefresh, refreshAccessToken, rotateToken, type KcToken, type OidcConfig } from "@/lib/token-refresh";

declare module "next-auth" {
  interface Session {
    /** Set when the access token could not be refreshed; the user has to sign in again. */
    error?: string;
  }
}

// Optional OIDC login (Keycloak or any OIDC issuer). Without OIDC_ISSUER the app runs in local mode:
// no login, and the API sees every request as the local user.
export const oidcEnabled = Boolean(process.env.OIDC_ISSUER);

const oidc = (): OidcConfig => ({
  issuer: process.env.OIDC_ISSUER ?? "",
  clientId: process.env.OIDC_CLIENT_ID ?? "",
  clientSecret: process.env.OIDC_CLIENT_SECRET,
});

export const { handlers, auth, signIn, signOut } = NextAuth({
  providers: oidcEnabled
    ? [
        Keycloak({
          issuer: process.env.OIDC_ISSUER,
          clientId: process.env.OIDC_CLIENT_ID,
          clientSecret: process.env.OIDC_CLIENT_SECRET,
        }),
      ]
    : [],
  secret: process.env.AUTH_SECRET,
  trustHost: true,
  session: { strategy: "jwt" },
  callbacks: {
    // Keep the access and refresh tokens in the encrypted JWT cookie only; they never reach the
    // browser session. The access token is renewed shortly before it expires (Keycloak: 900 s).
    async jwt({ token, account }) {
      return (await rotateToken(token as KcToken, account, oidc())) as typeof token;
    },
    // Only the refresh error is exposed, so pages and the proxy can send the user back to sign-in.
    session({ session, token }) {
      const error = (token as KcToken).error;
      if (error) session.error = error;
      return session;
    },
  },
});

/** The caller's OIDC access token, for the Authorization header to the API; undefined in local mode. */
export async function accessToken(): Promise<string | undefined> {
  if (!oidcEnabled) return undefined;
  const h = await headers();
  const token = (await getToken({
    req: { headers: h },
    secret: process.env.AUTH_SECRET,
    secureCookie: (process.env.AUTH_URL ?? "").startsWith("https://"),
  })) as KcToken | null;
  if (!token || token.error) return undefined;
  // The proxy's auth() call refreshes and re-issues the cookie, but this request still carries the
  // old cookie; refresh here too (calls for the same refresh token are shared) so it is not sent stale.
  const fresh = needsRefresh(token) ? await refreshAccessToken(token, oidc()) : token;
  return typeof fresh.access_token === "string" && !fresh.error ? fresh.access_token : undefined;
}
