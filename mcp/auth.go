package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/modelcontextprotocol/go-sdk/auth"
	"github.com/modelcontextprotocol/go-sdk/oauthex"
)

// OAuth 2.1 protected resource for the Streamable HTTP transport (MCP authorization spec).
//
// The server validates bearer JWTs from one OIDC issuer (Keycloak) with the issuer's JWKS, serves
// the protected-resource metadata (RFC 9728) at /.well-known/oauth-protected-resource and answers
// 401 with WWW-Authenticate: Bearer resource_metadata="..." when the token is missing or invalid.
// The client gets the token from the authorization server itself; this server never sees passwords.
//
// Environment: KG_OIDC_ISSUER (e.g. https://sso.example.com/realms/kg), KG_OIDC_AUDIENCE (the aud
// the token must carry, e.g. kg-mcp), KG_OIDC_GROUPS_CLAIM (default groups; dotted paths such as
// realm_access.roles work), KG_OIDC_JWKS_URL (optional; default from the issuer's discovery
// document), KG_OIDC_RESOURCE (public URL of /mcp; default from the request), KG_OIDC_SCOPES
// (scopes every call needs, comma-separated), KG_OIDC_WRITE_SCOPE (scope for the write-back tools).

const metadataPath = "/.well-known/oauth-protected-resource"

type OIDC struct {
	Issuer      string
	Audience    string
	GroupsClaim string
	JWKSURL     string
	Resource    string
	Scopes      []string

	client     *http.Client
	ttl        time.Duration // keys are fetched again after this time
	minRefresh time.Duration // an unknown kid fetches again at most this often (key rotation)
	leeway     time.Duration

	// mu guards the fields below and JWKSURL. It is never held during an HTTP fetch.
	mu       sync.RWMutex
	keys     map[string]any // kid -> *rsa.PublicKey or *ecdsa.PublicKey
	fetched  time.Time      // last successful fetch
	tried    time.Time      // last fetch attempt
	inflight chan struct{}  // closed when the running fetch ends; nil when none runs
	fetchErr error          // result of the last fetch
}

func oidcFromEnv() *OIDC {
	issuer := strings.TrimRight(env("KG_OIDC_ISSUER", ""), "/")
	if issuer == "" {
		return nil
	}
	var scopes []string
	for _, s := range strings.Split(env("KG_OIDC_SCOPES", ""), ",") {
		if s = strings.TrimSpace(s); s != "" {
			scopes = append(scopes, s)
		}
	}
	return newOIDC(issuer, env("KG_OIDC_AUDIENCE", "kg-mcp"), env("KG_OIDC_GROUPS_CLAIM", "groups"),
		env("KG_OIDC_JWKS_URL", ""), env("KG_OIDC_RESOURCE", ""), scopes)
}

func newOIDC(issuer, audience, groupsClaim, jwksURL, resource string, scopes []string) *OIDC {
	return &OIDC{Issuer: issuer, Audience: audience, GroupsClaim: groupsClaim, JWKSURL: jwksURL, Resource: resource,
		Scopes: scopes, client: &http.Client{Timeout: 10 * time.Second}, ttl: 10 * time.Minute,
		minRefresh: 30 * time.Second, leeway: 30 * time.Second}
}

// Middleware returns 401 for requests without a valid token and puts the token info into the
// request context, where the MCP SDK passes it to each request (req.Extra.TokenInfo).
func (o *OIDC) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		opts := &auth.RequireBearerTokenOptions{ResourceMetadataURL: o.metadataURL(r), Scopes: o.Scopes, ClockSkew: o.leeway}
		auth.RequireBearerToken(o.Verify, opts)(next).ServeHTTP(w, r)
	})
}

// MetadataHandler serves the protected-resource metadata. The client reads authorization_servers
// and then discovers the issuer's endpoints (RFC 8414 / OIDC discovery).
func (o *OIDC) MetadataHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		md := &oauthex.ProtectedResourceMetadata{Resource: o.resource(r), AuthorizationServers: []string{o.Issuer},
			ScopesSupported: o.Scopes, BearerMethodsSupported: []string{"header"}, ResourceName: "Knowledge graph MCP server"}
		auth.ProtectedResourceMetadataHandler(md).ServeHTTP(w, r)
	})
}

func (o *OIDC) resource(r *http.Request) string {
	if o.Resource != "" {
		return o.Resource
	}
	return baseURL(r) + "/mcp"
}

// metadataURL is the RFC 9728 well-known URL of the resource: the path of the resource is
// appended (https://kg.example.com/.well-known/oauth-protected-resource/mcp).
func (o *OIDC) metadataURL(r *http.Request) string {
	u, err := url.Parse(o.resource(r))
	if err != nil || u.Host == "" {
		return baseURL(r) + metadataPath
	}
	return u.Scheme + "://" + u.Host + metadataPath + strings.TrimRight(u.Path, "/")
}

func baseURL(r *http.Request) string {
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if p := r.Header.Get("X-Forwarded-Proto"); p == "https" || p == "http" {
		scheme = p
	}
	return scheme + "://" + r.Host
}

// Verify checks signature (JWKS), issuer, audience and expiry, and returns the caller's name and
// groups in TokenInfo.Extra.
func (o *OIDC) Verify(ctx context.Context, token string, _ *http.Request) (*auth.TokenInfo, error) {
	claims := jwt.MapClaims{}
	_, err := jwt.ParseWithClaims(token, claims, func(t *jwt.Token) (any, error) {
		kid, _ := t.Header["kid"].(string)
		return o.key(ctx, kid)
	}, jwt.WithValidMethods([]string{"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}),
		jwt.WithIssuer(o.Issuer), jwt.WithAudience(o.Audience), jwt.WithExpirationRequired(), jwt.WithLeeway(o.leeway))
	if err != nil {
		return nil, fmt.Errorf("%w: %v", auth.ErrInvalidToken, err)
	}
	exp, _ := claims.GetExpirationTime()
	sub, _ := claims.GetSubject()
	user, _ := claims["preferred_username"].(string)
	if user == "" {
		user = sub
	}
	var scopes []string
	if sc, ok := claims["scope"].(string); ok {
		scopes = strings.Fields(sc)
	}
	return &auth.TokenInfo{Scopes: scopes, Expiration: exp.Time, UserID: sub,
		Extra: map[string]any{"user": user, "groups": claimStrings(claims, o.GroupsClaim)}}, nil
}

// claimStrings reads a string or string-list claim; "a.b" walks into nested objects.
func claimStrings(claims map[string]any, path string) []string {
	var v any = claims
	for _, part := range strings.Split(path, ".") {
		m, ok := v.(map[string]any)
		if !ok {
			return nil
		}
		v = m[part]
	}
	switch x := v.(type) {
	case string:
		return []string{x}
	case []any:
		out := []string{}
		for _, e := range x {
			if s, ok := e.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// ---- JWKS ----------------------------------------------------------------------------------------

// key returns the public key for kid. Cached keys are read under the read lock; the discovery and
// JWKS fetch runs without the lock, so a slow issuer never holds up tokens signed by a known key.
func (o *OIDC) key(ctx context.Context, kid string) (any, error) {
	o.mu.RLock()
	k, ok := o.lookup(kid)
	fresh := time.Since(o.fetched) < o.ttl
	o.mu.RUnlock()
	if ok && fresh {
		return k, nil
	}
	err := o.refresh(ctx, !ok) // a known but old key goes on while the fetch runs
	o.mu.RLock()
	defer o.mu.RUnlock()
	if k, ok := o.lookup(kid); ok { // also when the issuer is down: keep the known keys
		return k, nil
	}
	if err != nil {
		return nil, err
	}
	if o.keys == nil {
		return nil, errors.New("jwks: signing keys not available yet")
	}
	return nil, fmt.Errorf("unknown signing key %q", kid)
}

// refresh starts a fetch of the keys unless one runs already or the last attempt was less than
// minRefresh ago (an unknown kid must not make every request hit the issuer). Only one fetch runs
// at a time; wait says whether to wait for it (the caller has no usable key).
func (o *OIDC) refresh(ctx context.Context, wait bool) error {
	o.mu.Lock()
	done := o.inflight
	if done == nil {
		if time.Since(o.tried) < o.minRefresh {
			o.mu.Unlock()
			return nil
		}
		o.tried = time.Now()
		done = make(chan struct{})
		o.inflight = done
		// the fetch belongs to all waiting requests, so the first one's cancellation must not end it;
		// the client timeout bounds it
		go o.fetchInto(context.WithoutCancel(ctx), o.JWKSURL, done)
	}
	o.mu.Unlock()
	if !wait {
		return nil
	}
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	o.mu.RLock()
	defer o.mu.RUnlock()
	return o.fetchErr
}

// fetchInto fetches the keys without the lock and stores them.
func (o *OIDC) fetchInto(ctx context.Context, jwksURL string, done chan struct{}) {
	keys, jwksURL, err := o.fetch(ctx, jwksURL)
	o.mu.Lock()
	if jwksURL != "" {
		o.JWKSURL = jwksURL
	}
	if err == nil {
		o.keys, o.fetched = keys, time.Now()
	}
	o.fetchErr, o.inflight = err, nil
	o.mu.Unlock()
	close(done)
}

func (o *OIDC) lookup(kid string) (any, bool) {
	if k, ok := o.keys[kid]; ok {
		return k, true
	}
	if kid == "" && len(o.keys) == 1 { // a token without kid works when the issuer has one key
		for _, k := range o.keys {
			return k, true
		}
	}
	return nil, false
}

// fetch reads the JWKS (from the issuer's discovery document when jwksURL is empty) and returns
// the signing keys and the JWKS URL it used. It touches no state of o.
func (o *OIDC) fetch(ctx context.Context, jwksURL string) (map[string]any, string, error) {
	if jwksURL == "" {
		var doc struct {
			Issuer  string `json:"issuer"`
			JWKSURI string `json:"jwks_uri"`
		}
		if err := o.getJSON(ctx, o.Issuer+"/.well-known/openid-configuration", &doc); err != nil {
			return nil, "", err
		}
		if doc.Issuer != o.Issuer || doc.JWKSURI == "" {
			return nil, "", fmt.Errorf("oidc discovery: issuer %q does not match %q or has no jwks_uri", doc.Issuer, o.Issuer)
		}
		jwksURL = doc.JWKSURI
	}
	var set struct {
		Keys []jwk `json:"keys"`
	}
	if err := o.getJSON(ctx, jwksURL, &set); err != nil {
		return nil, jwksURL, err
	}
	keys := map[string]any{}
	for _, k := range set.Keys {
		if k.Use != "" && k.Use != "sig" {
			continue // Keycloak also publishes encryption keys
		}
		if pub, err := k.public(); err == nil {
			keys[k.Kid] = pub
		}
	}
	if len(keys) == 0 {
		return nil, jwksURL, errors.New("jwks: no usable signing keys")
	}
	return keys, jwksURL, nil
}

func (o *OIDC) getJSON(ctx context.Context, u string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return err
	}
	resp, err := o.client.Do(req)
	if err != nil {
		return fmt.Errorf("oidc: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("oidc: GET %s: HTTP %d", u, resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

type jwk struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use"`
	N   string `json:"n"`
	E   string `json:"e"`
	Crv string `json:"crv"`
	X   string `json:"x"`
	Y   string `json:"y"`
}

func (k jwk) public() (any, error) {
	b := func(s string) (*big.Int, error) {
		raw, err := base64.RawURLEncoding.DecodeString(s)
		return new(big.Int).SetBytes(raw), err
	}
	switch k.Kty {
	case "RSA":
		n, err1 := b(k.N)
		e, err2 := b(k.E)
		if err := errors.Join(err1, err2); err != nil || n.BitLen() < 2048 {
			return nil, fmt.Errorf("jwk %s: bad RSA key", k.Kid)
		}
		return &rsa.PublicKey{N: n, E: int(e.Int64())}, nil
	case "EC":
		curve := map[string]elliptic.Curve{"P-256": elliptic.P256(), "P-384": elliptic.P384(), "P-521": elliptic.P521()}[k.Crv]
		x, err1 := b(k.X)
		y, err2 := b(k.Y)
		if curve == nil || errors.Join(err1, err2) != nil || !curve.IsOnCurve(x, y) {
			return nil, fmt.Errorf("jwk %s: bad EC key", k.Kid)
		}
		return &ecdsa.PublicKey{Curve: curve, X: x, Y: y}, nil
	}
	return nil, fmt.Errorf("jwk %s: unsupported kty %q", k.Kid, k.Kty)
}
