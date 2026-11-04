// Keeping the Keycloak access token fresh. Keycloak access tokens are short-lived (900 s by default),
// so the JWT cookie stores the refresh token and expiry too, and the token is renewed with the
// refresh_token grant shortly before it expires. When renewal fails the token carries an error and
// the app sends the user back to sign-in instead of failing every API call with 401.

export const REFRESH_ERROR = "RefreshAccessTokenError";

/** Renew this many seconds before the access token expires. */
export const REFRESH_MARGIN_S = 60;

export interface KcToken {
  access_token?: string;
  refresh_token?: string;
  /** Access token expiry, seconds since the epoch. */
  expires_at?: number;
  error?: string;
  [k: string]: unknown;
}

export interface OidcConfig {
  issuer: string;
  clientId: string;
  clientSecret?: string;
}

export const nowS = () => Math.floor(Date.now() / 1000);

/** Keycloak's token endpoint for a realm issuer URL. */
export const tokenEndpoint = (issuer: string) => `${issuer.replace(/\/$/, "")}/protocol/openid-connect/token`;

/** True when the access token has a known expiry that has passed or is within the margin. */
export function needsRefresh(token: KcToken, now = nowS()): boolean {
  if (!token.access_token) return false;
  if (typeof token.expires_at !== "number") return false;
  return now >= token.expires_at - REFRESH_MARGIN_S;
}

// Concurrent requests that see the same expiring token share one refresh call, so a rotating
// refresh token is not redeemed twice. Entries are dropped once settled.
const inflight = new Map<string, Promise<KcToken>>();

/** Exchange the refresh token for a new access token; returns the updated token, or one with `error` set. */
export function refreshAccessToken(
  token: KcToken,
  cfg: OidcConfig,
  fetchImpl: typeof fetch = fetch,
  now: () => number = nowS,
): Promise<KcToken> {
  const rt = token.refresh_token;
  if (!rt) return Promise.resolve(failed(token));
  const pending = inflight.get(rt);
  if (pending) return pending;
  const p = doRefresh(token, rt, cfg, fetchImpl, now).finally(() => inflight.delete(rt));
  inflight.set(rt, p);
  return p;
}

async function doRefresh(token: KcToken, rt: string, cfg: OidcConfig, fetchImpl: typeof fetch, now: () => number): Promise<KcToken> {
  const body = new URLSearchParams({ grant_type: "refresh_token", refresh_token: rt, client_id: cfg.clientId });
  if (cfg.clientSecret) body.set("client_secret", cfg.clientSecret);
  try {
    const res = await fetchImpl(tokenEndpoint(cfg.issuer), {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    if (!res.ok || typeof data.access_token !== "string") return failed(token);
    const expiresIn = typeof data.expires_in === "number" ? data.expires_in : 300;
    const next: KcToken = {
      ...token,
      access_token: data.access_token,
      expires_at: now() + expiresIn,
      // Keycloak may rotate the refresh token; keep the old one if it does not send a new one.
      refresh_token: typeof data.refresh_token === "string" ? data.refresh_token : rt,
    };
    delete next.error;
    return next;
  } catch {
    return failed(token);
  }
}

function failed(token: KcToken): KcToken {
  // Drop the dead access token so nothing keeps sending it.
  const { access_token: _a, ...rest } = token;
  return { ...rest, error: REFRESH_ERROR };
}

/** The jwt-callback step: store the tokens at sign-in, refresh near expiry, otherwise pass through. */
export async function rotateToken(
  token: KcToken,
  account: { access_token?: string; refresh_token?: string; expires_at?: number; expires_in?: number } | null | undefined,
  cfg: OidcConfig,
  fetchImpl: typeof fetch = fetch,
  now: () => number = nowS,
): Promise<KcToken> {
  if (account?.access_token) {
    const expiresAt =
      typeof account.expires_at === "number"
        ? account.expires_at
        : typeof account.expires_in === "number"
          ? now() + account.expires_in
          : undefined;
    const next: KcToken = { ...token, access_token: account.access_token, refresh_token: account.refresh_token, expires_at: expiresAt };
    delete next.error;
    return next;
  }
  if (token.error) return token;
  if (!needsRefresh(token, now())) return token;
  return refreshAccessToken(token, cfg, fetchImpl, now);
}
