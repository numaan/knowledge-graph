package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
)

// ---- test identity provider: discovery document, JWKS and signed tokens --------------------------

type testIdP struct {
	t       *testing.T
	srv     *httptest.Server
	mu      sync.Mutex
	keys    map[string]any // kid -> private key
	fetches int
}

func newTestIdP(t *testing.T) *testIdP {
	idp := &testIdP{t: t, keys: map[string]any{}}
	idp.addRSA("k1")
	mux := http.NewServeMux()
	mux.HandleFunc("/realms/kg/.well-known/openid-configuration", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"issuer": idp.issuer(), "jwks_uri": idp.issuer() + "/protocol/openid-connect/certs"})
	})
	mux.HandleFunc("/realms/kg/protocol/openid-connect/certs", func(w http.ResponseWriter, r *http.Request) {
		idp.mu.Lock()
		defer idp.mu.Unlock()
		idp.fetches++
		keys := []map[string]string{{"kty": "RSA", "kid": "enc", "use": "enc", "n": "AQAB", "e": "AQAB"}} // must be skipped
		b64 := func(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }
		for kid, k := range idp.keys {
			switch p := k.(type) {
			case *rsa.PrivateKey:
				keys = append(keys, map[string]string{"kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
					"n": b64(p.N.Bytes()), "e": b64(big.NewInt(int64(p.E)).Bytes())})
			case *ecdsa.PrivateKey:
				keys = append(keys, map[string]string{"kty": "EC", "kid": kid, "use": "sig", "crv": "P-256",
					"x": b64(p.X.FillBytes(make([]byte, 32))), "y": b64(p.Y.FillBytes(make([]byte, 32)))})
			}
		}
		json.NewEncoder(w).Encode(map[string]any{"keys": keys})
	})
	idp.srv = httptest.NewServer(mux)
	t.Cleanup(idp.srv.Close)
	return idp
}

func (idp *testIdP) issuer() string { return idp.srv.URL + "/realms/kg" }

func (idp *testIdP) addRSA(kid string) {
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		idp.t.Fatal(err)
	}
	idp.mu.Lock()
	idp.keys[kid] = k
	idp.mu.Unlock()
}

func (idp *testIdP) addEC(kid string) {
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		idp.t.Fatal(err)
	}
	idp.mu.Lock()
	idp.keys[kid] = k
	idp.mu.Unlock()
}

// token signs claims with the key kid; base claims are a valid token for alice.
func (idp *testIdP) token(kid string, change func(jwt.MapClaims)) string {
	c := jwt.MapClaims{"iss": idp.issuer(), "aud": []string{"kg-mcp", "account"}, "sub": "u-alice",
		"preferred_username": "alice", "exp": time.Now().Add(5 * time.Minute).Unix(), "iat": time.Now().Unix(),
		"groups": []string{"/team-precept"}, "scope": "openid kg:write"}
	if change != nil {
		change(c)
	}
	idp.mu.Lock()
	key := idp.keys[kid]
	idp.mu.Unlock()
	method := jwt.SigningMethod(jwt.SigningMethodRS256)
	if _, ok := key.(*ecdsa.PrivateKey); ok {
		method = jwt.SigningMethodES256
	}
	t := jwt.NewWithClaims(method, c)
	t.Header["kid"] = kid
	s, err := t.SignedString(key)
	if err != nil {
		idp.t.Fatal(err)
	}
	return s
}

func TestOIDCVerify(t *testing.T) {
	idp := newTestIdP(t)
	o := newOIDC(idp.issuer(), "kg-mcp", "groups", "", "", nil)
	ctx := context.Background()

	info, err := o.Verify(ctx, idp.token("k1", nil), nil)
	if err != nil {
		t.Fatal(err)
	}
	if info.Extra["user"] != "alice" || info.UserID != "u-alice" || info.Expiration.IsZero() {
		t.Errorf("token info: %+v", info)
	}
	if g := info.Extra["groups"].([]string); len(g) != 1 || g[0] != "/team-precept" {
		t.Errorf("groups: %v", g)
	}
	if len(info.Scopes) != 2 || info.Scopes[1] != "kg:write" {
		t.Errorf("scopes: %v", info.Scopes)
	}

	other := newTestIdP(t) // same claims, signed by a key the issuer does not publish
	bad := map[string]string{
		"wrong audience": idp.token("k1", func(c jwt.MapClaims) { c["aud"] = "account" }),
		"wrong issuer":   idp.token("k1", func(c jwt.MapClaims) { c["iss"] = "https://evil.example.com/realms/kg" }),
		"expired":        idp.token("k1", func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Hour).Unix() }),
		"no expiry":      idp.token("k1", func(c jwt.MapClaims) { delete(c, "exp") }),
		"other key":      other.token("k1", func(c jwt.MapClaims) { c["iss"] = idp.issuer() }),
		"garbage":        "not.a.jwt",
		"alg none":       noneToken(jwt.MapClaims{"iss": idp.issuer(), "aud": "kg-mcp", "exp": time.Now().Add(time.Hour).Unix()}),
	}
	for name, tok := range bad {
		if _, err := o.Verify(ctx, tok, nil); err == nil {
			t.Errorf("%s: token accepted", name)
		}
	}

	// key rotation: a new kid triggers one JWKS fetch; the cache serves the rest
	idp.addEC("k2")
	o.minRefresh = 0
	before := idp.fetches
	for range 3 {
		if _, err := o.Verify(ctx, idp.token("k2", nil), nil); err != nil {
			t.Fatalf("rotated EC key: %v", err)
		}
	}
	if idp.fetches-before != 1 {
		t.Errorf("JWKS fetched %d times for a new key, want 1", idp.fetches-before)
	}
}

func noneToken(c jwt.MapClaims) string {
	s, _ := jwt.NewWithClaims(jwt.SigningMethodNone, c).SignedString(jwt.UnsafeAllowNoneSignatureType)
	return s
}

func TestClaimStrings(t *testing.T) {
	c := map[string]any{"groups": []any{"a", 1, "b"}, "realm_access": map[string]any{"roles": []any{"kg-admin"}}, "one": "x"}
	if g := claimStrings(c, "groups"); len(g) != 2 {
		t.Errorf("groups: %v", g)
	}
	if g := claimStrings(c, "realm_access.roles"); len(g) != 1 || g[0] != "kg-admin" {
		t.Errorf("nested: %v", g)
	}
	if g := claimStrings(c, "one"); len(g) != 1 || claimStrings(c, "missing.x") != nil {
		t.Errorf("string or missing claim wrong")
	}
}

// ---- ACL mapping and bounds ----------------------------------------------------------------------

func TestACLMappingAndBound(t *testing.T) {
	path := filepath.Join(t.TempDir(), "acl-mapping.yaml")
	os.WriteFile(path, []byte(`
default: [docs:handbook]
groups:
  kg-admins: ["*"]
  Team-Precept: [repo:precept, jira:PREC, confluence:ENG]
  team-hr: [jira:HR, confluence:HR]
`), 0o600)
	m, err := loadACLMapping(path)
	if err != nil {
		t.Fatal(err)
	}
	got := m.resolve([]string{"/team-precept", "/repo:falcon", "/Jira-Group:Precept-Devs", "unrelated"})
	for _, want := range []string{"docs:handbook", "repo:precept", "jira:PREC", "confluence:ENG", "repo:falcon", "jira-group:precept-devs"} {
		if !got[want] {
			t.Errorf("resolve: missing %s in %v", want, sortedGroups(got))
		}
	}
	if got["unrelated"] || len(got) != 6 {
		t.Errorf("resolve: %v", sortedGroups(got))
	}

	upper := map[string]bool{"repo:precept": true, "jira:PREC": true, "jira:HR": true}
	if b := bound(map[string]bool{"repo:precept": true, "repo:falcon": true}, upper); len(b) != 1 || !b["repo:precept"] {
		t.Errorf("bound must drop groups above the server groups: %v", b)
	}
	if b := bound(map[string]bool{"*": true}, upper); len(b) != 3 || b["*"] {
		t.Errorf("an admin token gets exactly the server groups: %v", b)
	}
	if b := bound(map[string]bool{"*": true}, map[string]bool{"*": true}); !b["*"] {
		t.Error("* within * stays *")
	}
	if _, err := loadACLMapping(filepath.Join(t.TempDir(), "missing.yaml")); err == nil {
		t.Error("missing mapping file must fail")
	}
}

func TestGroupsFromContext(t *testing.T) {
	s := &Server{groups: map[string]bool{"*": true}}
	alice := withAccess(context.Background(), &Access{User: "alice", Groups: map[string]bool{"jira:PREC": true}})
	if s.allowed(alice, []any{"jira:HR"}) || !s.allowed(alice, []any{"jira:PREC"}) || s.allowedRepo(alice, "precept") {
		t.Error("the caller's groups must replace the server groups")
	}
	if !s.allowed(context.Background(), []any{"jira:HR"}) {
		t.Error("without a caller, stdio uses the server groups")
	}
	s.requireCaller = true
	if s.allowed(context.Background(), []any{"jira:HR"}) || s.allowedRepo(context.Background(), "precept") {
		t.Error("OIDC mode: no caller must see no protected data")
	}
	if got := repoScope(accessFrom(alice).Groups).([]string); len(got) != 0 {
		t.Errorf("alice has no repo groups: %v", got)
	}
	if accessFrom(alice).stats.hidden.Load() != 1 {
		t.Errorf("hidden count: %d", accessFrom(alice).stats.hidden.Load())
	}
}

// ---- end to end over Streamable HTTP with tokens --------------------------------------------------

type fakeBackend struct {
	mu        sync.Mutex
	nodes     map[string][2]any // id -> {repo, acl}
	edges     map[string]bool   // "src|TYPE|dst"
	proposals []map[string]any
}

func (f *fakeBackend) NodeAccess(_ context.Context, id string) (bool, string, any, error) {
	n, ok := f.nodes[id]
	if !ok {
		return false, "", nil, nil
	}
	return true, n[0].(string), n[1], nil
}

func (f *fakeBackend) EdgeExists(_ context.Context, src, t, dst string) (bool, error) {
	return f.edges[src+"|"+t+"|"+dst], nil
}

func (f *fakeBackend) CreateProposal(_ context.Context, p map[string]any) (string, string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, old := range f.proposals {
		if old["content_hash"] == p["content_hash"] {
			return old["id"].(string), "open", false, nil
		}
	}
	f.proposals = append(f.proposals, p)
	return p["id"].(string), "open", true, nil
}

func newFakeBackend() *fakeBackend {
	return &fakeBackend{
		nodes: map[string][2]any{
			"precept:app.py#Invite": {"precept", nil},
			"falcon:x.go#Run":       {"falcon", nil},
			"domain:precept:invite": {"precept", nil},
			"jira:PREC-1":           {"", []any{"jira:PREC"}},
			"jira:HR-7":             {"", []any{"jira:HR"}},
		},
		edges: map[string]bool{"precept:app.py#Invite|REALISES|domain:precept:invite": true},
	}
}

type bearer struct{ token string }

func (b bearer) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	if b.token != "" {
		r.Header.Set("Authorization", "Bearer "+b.token)
	}
	return http.DefaultTransport.RoundTrip(r)
}

type govEnv struct {
	url     string
	backend *fakeBackend
	audit   *bytes.Buffer
	idp     *testIdP
}

func newGovEnv(t *testing.T) *govEnv {
	idp := newTestIdP(t)
	path := filepath.Join(t.TempDir(), "acl-mapping.yaml")
	os.WriteFile(path, []byte("groups:\n  team-precept: [repo:precept, jira:PREC]\n  team-hr: [jira:HR]\n  kg-admins: ['*']\n"), 0o600)
	mapping, err := loadACLMapping(path)
	if err != nil {
		t.Fatal(err)
	}
	e := &govEnv{backend: newFakeBackend(), audit: &bytes.Buffer{}, idp: idp}
	s := &Server{groups: map[string]bool{"*": true}}
	s.mapping, s.requireCaller, s.proposals = mapping, true, e.backend
	s.audit = &auditLog{out: []io.Writer{e.audit}}
	oidc := newOIDC(idp.issuer(), "kg-mcp", "groups", "", "", nil)
	server := newMCPServer(s)
	mux := http.NewServeMux()
	mux.Handle("/mcp", oidc.Middleware(mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)))
	mux.Handle(metadataPath+"/mcp", oidc.MetadataHandler())
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	e.url = srv.URL
	return e
}

func (e *govEnv) client(t *testing.T, token string) *mcp.ClientSession {
	t.Helper()
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(context.Background(),
		&mcp.StreamableClientTransport{Endpoint: e.url + "/mcp", HTTPClient: &http.Client{Transport: bearer{token}},
			MaxRetries: -1, DisableStandaloneSSE: true}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

func (e *govEnv) auditLines(t *testing.T) []auditRecord {
	var out []auditRecord
	for _, line := range strings.Split(strings.TrimSpace(e.audit.String()), "\n") {
		if line == "" {
			continue
		}
		var r auditRecord
		if err := json.Unmarshal([]byte(line), &r); err != nil {
			t.Fatalf("audit line is not JSON: %s", line)
		}
		out = append(out, r)
	}
	return out
}

func TestHTTPUnauthorized(t *testing.T) {
	e := newGovEnv(t)
	for name, tok := range map[string]string{"no token": "", "bad token": "abc.def.ghi",
		"wrong audience": e.idp.token("k1", func(c jwt.MapClaims) { c["aud"] = "other" })} {
		req, _ := http.NewRequest(http.MethodPost, e.url+"/mcp", strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"tools/list"}`))
		if tok != "" {
			req.Header.Set("Authorization", "Bearer "+tok)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		want := `Bearer resource_metadata="` + e.url + `/.well-known/oauth-protected-resource/mcp"`
		if resp.StatusCode != http.StatusUnauthorized || resp.Header.Get("WWW-Authenticate") != want {
			t.Errorf("%s: %d %q", name, resp.StatusCode, resp.Header.Get("WWW-Authenticate"))
		}
	}
	resp, err := http.Get(e.url + metadataPath + "/mcp")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var md map[string]any
	json.NewDecoder(resp.Body).Decode(&md)
	if md["resource"] != e.url+"/mcp" || md["authorization_servers"].([]any)[0] != e.idp.issuer() {
		t.Errorf("metadata: %v", md)
	}
}

func callTool(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any) (*mcp.CallToolResult, string) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	text := ""
	if len(res.Content) > 0 {
		if tc, ok := res.Content[0].(*mcp.TextContent); ok {
			text = tc.Text
		}
	}
	return res, text
}

// Per-user ACL: the same server, three callers with different groups.
func TestPerUserACLOverHTTP(t *testing.T) {
	e := newGovEnv(t)
	alice := e.client(t, e.idp.token("k1", nil))
	bob := e.client(t, e.idp.token("k1", func(c jwt.MapClaims) {
		c["preferred_username"], c["sub"], c["groups"] = "bob", "u-bob", []string{"/team-hr"}
	}))
	admin := e.client(t, e.idp.token("k1", func(c jwt.MapClaims) {
		c["preferred_username"], c["sub"], c["groups"] = "root", "u-root", []string{"/kg-admins"}
	}))

	// alice (team-precept) can report on precept code and PREC tickets, not on HR or falcon
	if res, text := callTool(t, alice, "report_wrong_fact", map[string]any{"node_id": "jira:PREC-1", "reason": "fixed in 2.3"}); res.IsError {
		t.Errorf("alice on PREC-1: %s", text)
	}
	for _, id := range []string{"jira:HR-7", "falcon:x.go#Run"} {
		res, text := callTool(t, alice, "report_wrong_fact", map[string]any{"node_id": id, "reason": "x"})
		if !res.IsError || !strings.Contains(text, "not found or not visible") {
			t.Errorf("alice on %s: allowed (%s)", id, text)
		}
	}
	if res, _ := callTool(t, alice, "report_wrong_fact", map[string]any{"node_id": "jira:NOPE-1", "reason": "x"}); !res.IsError {
		t.Error("unknown node accepted")
	}
	// bob (team-hr) sees HR only
	if res, text := callTool(t, bob, "record_decision", map[string]any{"text": "HR data stays in HR", "links": []string{"jira:HR-7"}}); res.IsError {
		t.Errorf("bob on HR-7: %s", text)
	}
	if res, _ := callTool(t, bob, "suggest_link", map[string]any{"src": "precept:app.py#Invite", "edge_type": "REALISES", "dst": "domain:precept:invite"}); !res.IsError {
		t.Error("bob suggested a link between precept nodes")
	}
	// read_cypher stays off for everyone without *
	for name, cs := range map[string]*mcp.ClientSession{"alice": alice, "bob": bob} {
		if res, text := callTool(t, cs, "read_cypher", map[string]any{"query": "MATCH (n) RETURN n"}); !res.IsError || !strings.Contains(text, "off") {
			t.Errorf("read_cypher for %s: %s", name, text)
		}
	}
	// the admin sees everything; the edge must exist for a wrong-edge report
	if res, text := callTool(t, admin, "report_wrong_fact", map[string]any{"edge_src": "precept:app.py#Invite",
		"edge_type": "REALISES", "edge_dst": "domain:precept:invite", "reason": "Invite does not realise this concept"}); res.IsError {
		t.Errorf("admin edge report: %s", text)
	}
	if res, _ := callTool(t, admin, "report_wrong_fact", map[string]any{"edge_src": "precept:app.py#Invite",
		"edge_type": "CALLS", "edge_dst": "domain:precept:invite", "reason": "x"}); !res.IsError {
		t.Error("report on a missing edge accepted")
	}

	if len(e.backend.proposals) != 3 {
		t.Fatalf("want 3 proposals, got %d", len(e.backend.proposals))
	}
	p := e.backend.proposals[0]
	if p["author"] != "alice" || p["author_via"] != "token" || p["kind"] != "wrong_fact" || p["status"] != "open" || p["target_id"] != "jira:PREC-1" {
		t.Errorf("proposal: %v", p)
	}
	if e.backend.proposals[2]["edge_type"] != "REALISES" || e.backend.proposals[2]["other_id"] != "domain:precept:invite" {
		t.Errorf("edge proposal: %v", e.backend.proposals[2])
	}
	// the same report again is stored once
	res, _ := callTool(t, alice, "report_wrong_fact", map[string]any{"node_id": "jira:PREC-1", "reason": "fixed in 2.3"})
	var out ProposalOut
	b, _ := json.Marshal(res.StructuredContent)
	json.Unmarshal(b, &out)
	if out.Created || len(e.backend.proposals) != 3 {
		t.Errorf("duplicate proposal stored: %+v", out)
	}

	// audit: one line per call, with user, groups and the denied flag, and no free text
	lines := e.auditLines(t)
	if len(lines) != 11 {
		t.Fatalf("want 11 audit lines, got %d:\n%s", len(lines), e.audit.String())
	}
	first := lines[0]
	if first.User != "alice" || first.Tool != "report_wrong_fact" || first.Denied || first.Results != 1 ||
		strings.Join(first.Groups, ",") != "jira:PREC,repo:precept" || first.ArgsHash == "" {
		t.Errorf("audit line: %+v", first)
	}
	if !lines[1].Denied || !lines[1].Error || lines[3].Denied {
		t.Errorf("denied flags: %+v / %+v", lines[1], lines[3])
	}
	if strings.Contains(e.audit.String(), "fixed in 2.3") || strings.Contains(e.audit.String(), "Bearer") {
		t.Error("audit log holds free text or a token")
	}
}

func TestWriteScope(t *testing.T) {
	s := &Server{groups: map[string]bool{"*": true}}
	s.writeScope = "kg:write"
	ctx := withAccess(context.Background(), &Access{User: "a", Groups: map[string]bool{"*": true}, Via: "token", Scopes: []string{"openid"}})
	if s.canWrite(ctx) == nil {
		t.Error("write without the scope")
	}
	ctx = withAccess(context.Background(), &Access{User: "a", Groups: map[string]bool{"*": true}, Via: "token", Scopes: []string{"kg:write"}})
	if err := s.canWrite(ctx); err != nil {
		t.Error(err)
	}
}

func TestFeedbackSink(t *testing.T) {
	f := newFakeBackend()
	s := &Server{groups: map[string]bool{"*": true}}
	s.proposals = f
	ctx := withAccess(context.Background(), &Access{User: "carol", Groups: map[string]bool{"jira:PREC": true}, Via: "token"})
	sink := s.feedbackSink()
	if err := sink.Submit(ctx, Feedback{NodeID: "jira:PREC-1", Kind: "outdated", Comment: "superseded", Caller: &Caller{Subject: "mallory"}}); err != nil {
		t.Fatal(err)
	}
	p := f.proposals[0]
	if p["author"] != "carol" || p["kind"] != "feedback" || p["feedback_kind"] != "outdated" || p["source"] != "rest" {
		t.Errorf("feedback proposal: %v", p)
	}
	for _, bad := range []Feedback{{NodeID: "jira:HR-7", Kind: "wrong"}, {NodeID: "", Kind: "wrong"},
		{NodeID: "jira:PREC-1", Kind: "DROP TABLE"}, {NodeID: "jira:PREC-1", Kind: "wrong", Comment: strings.Repeat("x", maxReason+1)}} {
		if sink.Submit(ctx, bad) == nil {
			t.Errorf("feedback accepted: %+v", bad)
		}
	}
	// a REST caller without an Access in the context: OIDC mode maps its token groups
	s.requireCaller = true
	s.mapping = &aclMapping{Groups: map[string][]string{"team-hr": {"jira:HR"}}}
	rest := WithCaller(context.Background(), &Caller{Subject: "u-dana", Name: "dana", Groups: []string{"/team-hr"}})
	if err := sink.Submit(rest, Feedback{NodeID: "jira:HR-7", Kind: "wrong"}); err != nil {
		t.Fatal(err)
	}
	if p := f.proposals[1]; p["author"] != "dana" || p["author_via"] != "rest" {
		t.Errorf("REST feedback: %v", p)
	}
	if sink.Submit(rest, Feedback{NodeID: "jira:PREC-1", Kind: "wrong"}) == nil {
		t.Error("REST caller wrote feedback on a node outside its groups")
	}
}

// The access middleware takes the REST caller (rest.go puts it into the context) like a token.
func TestMiddlewareRESTCaller(t *testing.T) {
	s := &Server{groups: map[string]bool{"jira:HR": true, "jira:PREC": true}}
	s.requireCaller = true
	s.mapping = &aclMapping{Groups: map[string][]string{"team-hr": {"jira:HR", "repo:other"}}}
	var got *Access
	h := s.accessMiddleware(func(ctx context.Context, _ string, _ mcp.Request) (mcp.Result, error) {
		got = accessFrom(ctx)
		return &mcp.CallToolResult{}, nil
	})
	req := &mcp.CallToolRequest{Params: &mcp.CallToolParamsRaw{Name: "search_knowledge"}}
	ctx := WithCaller(context.Background(), &Caller{Subject: "u-dana", Name: "dana", Groups: []string{"/team-hr"}})
	if _, err := h(ctx, "tools/call", req); err != nil || got == nil {
		t.Fatal(err)
	}
	if got.User != "dana" || got.Via != "rest" || strings.Join(sortedGroups(got.Groups), ",") != "jira:HR" {
		t.Errorf("REST access: %+v", got)
	}
	got = nil
	if _, err := h(context.Background(), "tools/call", req); err == nil || got != nil {
		t.Error("OIDC mode ran a call without a caller")
	}
}

// ---- guard tests for write-back -------------------------------------------------------------------

func TestCheckProposalWrite(t *testing.T) {
	if err := checkProposalWrite(proposalStatement); err != nil {
		t.Fatalf("the proposal statement is rejected: %v", err)
	}
	for _, q := range []string{"CREATE (p:Proposal) SET p = $props", "create (p:Proposal {id: $id}) set p.status = 'open' return p"} {
		if err := checkProposalWrite(q); err != nil {
			t.Errorf("rejected %q: %v", q, err)
		}
	}
	bad := []string{
		"MATCH (n:Symbol) SET n.x = 1",
		"CREATE (p:Proposal)-[:ABOUT]->(n)",
		"CREATE (p:Proposal) <-[:X]- (n)",
		"CREATE (p:Proposal), (q:Correction)",
		"CREATE (n:Code {id: 'x'})",
		"MERGE (p:Proposal {id: 1}) SET p.a = 1, n.b = 2",
		"CREATE (p:Proposal) WITH p MATCH (n) DETACH DELETE n",
		"CREATE (p:Proposal:Code)",
		"CREATE (p:Proposal) REMOVE p.x",
		"UNWIND $rows AS r CREATE (p:Proposal)",
		"CALL apoc.create.node(['Code'], {}) YIELD node RETURN node",
		"RETURN 1",
	}
	for _, q := range bad {
		if err := checkProposalWrite(q); err == nil {
			t.Errorf("accepted %q", q)
		}
	}
	// read_cypher still rejects any write, a proposal too
	if checkReadOnly("CREATE (p:Proposal {id: 'x'})") == nil {
		t.Error("read_cypher accepted a proposal write")
	}
}

// Only writeProposal may open a write transaction; every other query goes through Graph.Read.
func TestOnlyProposalsWrite(t *testing.T) {
	writes := regexp.MustCompile(`AccessModeWrite|ExecuteWrite|ExecuteQuery|BeginTransaction|session\.Run\(`)
	files, _ := filepath.Glob("*.go")
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, _ := os.ReadFile(f)
		for _, m := range writes.FindAllIndex(b, -1) {
			fn := enclosingFunc(string(b[:m[0]]))
			if f != "writeback.go" || fn != "writeProposal" {
				t.Errorf("%s: %s in %s; only writeProposal may write", f, b[m[0]:m[1]], fn)
			}
		}
	}
	// and the server lists exactly three tools that are not read-only
	cs := (&govEnv{}).inMemory(t)
	tools, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var writers []string
	for _, tool := range tools.Tools {
		if tool.Annotations != nil && !tool.Annotations.ReadOnlyHint {
			writers = append(writers, tool.Name)
		}
	}
	if strings.Join(writers, ",") != "record_decision,report_wrong_fact,suggest_link" {
		t.Errorf("write tools: %v", writers)
	}
}

func enclosingFunc(before string) string {
	i := strings.LastIndex(before, "\nfunc ")
	if i < 0 {
		return ""
	}
	sig := before[i+6:]
	if strings.HasPrefix(sig, "(") {
		sig = sig[strings.Index(sig, ")")+1:]
	}
	sig = strings.TrimSpace(sig)
	return sig[:strings.IndexAny(sig, "([")]
}

func (*govEnv) inMemory(t *testing.T) *mcp.ClientSession {
	s := &Server{groups: map[string]bool{"*": true}}
	st, ct := mcp.NewInMemoryTransports()
	if _, err := newMCPServer(s).Connect(context.Background(), st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(context.Background(), ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cs.Close() })
	return cs
}

// ---- audit ---------------------------------------------------------------------------------------

func TestAuditSummary(t *testing.T) {
	args := json.RawMessage(`{"query":"secret plan for HR layoffs","repo":"precept","k":5,"edge_types":["CALLS","IMPORTS"],"params":{"a":1}}`)
	got := argsSummary(args)
	if got != "edge_types=[2] k=5 params={1} query=<26 chars> repo=precept" {
		t.Errorf("summary: %s", got)
	}
	if argsHash(args) == argsHash(json.RawMessage(`{}`)) || len(argsHash(args)) != 16 {
		t.Error("hash")
	}
	if resultCount(Rows{Results: []map[string]any{{}, {}}}) != 2 || resultCount(Doc{Data: map[string]any{"a": 1}}) != 1 || resultCount(Doc{}) != 0 {
		t.Error("result count")
	}
	t.Setenv("KG_AUDIT", "stdout")
	if _, err := newAuditLog(true); err == nil {
		t.Error("stdout audit in stdio mode must fail")
	}
}

// stdio keeps the env ACL: the middleware gives every call the server groups.
func TestStdioUsesEnvACL(t *testing.T) {
	f := newFakeBackend()
	buf := &bytes.Buffer{}
	t.Setenv("KG_USER", "dev")
	s := &Server{groups: map[string]bool{"jira:PREC": true}}
	s.proposals, s.audit = f, &auditLog{out: []io.Writer{buf}}
	st, ct := mcp.NewInMemoryTransports()
	if _, err := newMCPServer(s).Connect(context.Background(), st, nil); err != nil {
		t.Fatal(err)
	}
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "0"}, nil).Connect(context.Background(), ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()
	if res, text := callTool(t, cs, "report_wrong_fact", map[string]any{"node_id": "jira:PREC-1", "reason": "x"}); res.IsError {
		t.Error(text)
	}
	if res, _ := callTool(t, cs, "report_wrong_fact", map[string]any{"node_id": "jira:HR-7", "reason": "x"}); !res.IsError {
		t.Error("env ACL ignored")
	}
	if f.proposals[0]["author"] != "dev" || f.proposals[0]["author_via"] != "env" || !strings.Contains(buf.String(), `"user":"dev"`) {
		t.Errorf("stdio author: %v / %s", f.proposals[0], buf.String())
	}
}

// Neo4j: proposals are written by the write transaction; the same proposal is stored once; the
// node lookup sees the ACL of a section through its document.
func TestProposalsNeo4j(t *testing.T) {
	if os.Getenv("NEO4J_PASSWORD") == "" {
		t.Skip("NEO4J_PASSWORD not set")
	}
	ctx := context.Background()
	g, err := NewGraph(env("NEO4J_URI", "bolt://localhost:7687"), env("NEO4J_USER", "neo4j"), os.Getenv("NEO4J_PASSWORD"))
	if err != nil {
		t.Fatal(err)
	}
	defer g.Close(ctx)
	if err := g.Verify(ctx); err != nil {
		t.Skip("neo4j not reachable:", err)
	}
	setup := func(q string) {
		if _, err := neo4j.ExecuteQuery(ctx, g.driver, q, nil, neo4j.EagerResultTransformer); err != nil {
			t.Fatal(err)
		}
	}
	setup(`CREATE (:Source:Document {id: 'confluence:KGTGO:1', acl: ['confluence:KGTGO']})<-[:PART_OF]-(:Source:Section {id: 'confluence:KGTGO:1#s0'}),
	       (:Source:Ticket {id: 'jira:KGTGO-1', acl: ['jira:KGTGO']})-[:LINKS]->(:Source:Ticket {id: 'jira:KGTGO-2'})`)
	defer setup(`MATCH (n) WHERE n.id STARTS WITH 'confluence:KGTGO' OR n.id STARTS WITH 'jira:KGTGO' OR n.target_id STARTS WITH 'jira:KGTGO' DETACH DELETE n`)

	found, _, acl, err := g.NodeAccess(ctx, "confluence:KGTGO:1#s0")
	if err != nil || !found || acl.([]any)[0] != "confluence:KGTGO" {
		t.Fatalf("section acl through its document: %v %v %v", found, acl, err)
	}
	if ok, err := g.EdgeExists(ctx, "jira:KGTGO-1", "LINKS", "jira:KGTGO-2"); err != nil || !ok {
		t.Fatalf("edge: %v %v", ok, err)
	}
	props := proposalProps(map[string]any{"kind": "wrong_fact", "target_id": "jira:KGTGO-1", "text": "old"}, "alice", "token", "mcp")
	id, status, created, err := g.CreateProposal(ctx, props)
	if err != nil || !created || status != "open" || id != props["id"] {
		t.Fatalf("create: %v %v %v %v", id, status, created, err)
	}
	again := proposalProps(map[string]any{"kind": "wrong_fact", "target_id": "jira:KGTGO-1", "text": "old"}, "alice", "token", "mcp")
	if id2, _, created, _ := g.CreateProposal(ctx, again); created || id2 != id {
		t.Errorf("duplicate proposal created: %v", id2)
	}
	if _, err := g.writeProposal(ctx, "MATCH (n:Ticket) SET n.acl = []", nil); err == nil {
		t.Error("writeProposal ran a statement other than a proposal")
	}
	rows, _ := g.Read(ctx, "MATCH (p:Proposal {target_id: 'jira:KGTGO-1'}) RETURN count(p) AS n, collect(p.author)[0] AS a", nil)
	if rows[0]["n"].(int64) != 1 || rows[0]["a"] != "alice" {
		t.Errorf("proposals in the graph: %v", rows)
	}
}
