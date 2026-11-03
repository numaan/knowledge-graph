# Shared helpers for the hidden checks; copied next to each check as tests/bench_common.py.
def hdr(user, org=None):
    h = {"X-Dev-User": user}
    if org is not None:
        h["X-Dev-Org"] = org
    return h


def org(client, slug="bench-a", owner="a@x.io"):
    return client.post("/api/orgs", json={"name": slug.upper(), "slug": slug}, headers=hdr(owner)).json()


def invite(client, org_id, email, role="member", as_user="a@x.io", slug="bench-a"):
    return client.post(f"/api/orgs/{org_id}/invitations", json={"email": email, "role": role}, headers=hdr(as_user, slug))
