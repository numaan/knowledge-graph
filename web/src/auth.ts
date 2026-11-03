import NextAuth from "next-auth";
import { getToken } from "next-auth/jwt";
import Keycloak from "next-auth/providers/keycloak";
import { headers } from "next/headers";

// Optional OIDC login (Keycloak or any OIDC issuer). Without OIDC_ISSUER the app runs in local mode:
// no login, and the API sees every request as the local user.
export const oidcEnabled = Boolean(process.env.OIDC_ISSUER);

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
    // Keep the access token in the encrypted JWT cookie only; it never reaches the browser session.
    jwt({ token, account }) {
      if (account?.access_token) {
        token.access_token = account.access_token;
        token.expires_at = account.expires_at;
      }
      return token;
    },
  },
});

/** The caller's OIDC access token, for the Authorization header to the API; undefined in local mode. */
export async function accessToken(): Promise<string | undefined> {
  if (!oidcEnabled) return undefined;
  const h = await headers();
  const token = await getToken({
    req: { headers: h },
    secret: process.env.AUTH_SECRET,
    secureCookie: (process.env.AUTH_URL ?? "").startsWith("https://"),
  });
  return typeof token?.access_token === "string" ? token.access_token : undefined;
}
