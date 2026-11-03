from tests.bench_common import hdr, invite, org


def _revoke_events(client, org_id):
    rows = client.get(f"/api/orgs/{org_id}/audit", params={"action": "invite.revoke"}, headers=hdr("a@x.io", "bench-a")).json()
    return [r for r in rows if r["action"] == "invite.revoke"]


def test_revoke_with_reason_is_audited(client):
    a = org(client)
    inv = invite(client, a["id"], "bob@x.io").json()["invitation"]
    r = client.delete(f"/api/orgs/{a['id']}/invitations/{inv['id']}", params={"reason": "sent to the wrong address"},
                      headers=hdr("a@x.io", "bench-a"))
    assert r.status_code == 204
    events = _revoke_events(client, a["id"])
    assert events and events[0]["meta"].get("reason") == "sent to the wrong address"
    assert events[0]["meta"].get("email") == "bob@x.io"  # the existing field stays


def test_revoke_without_reason_still_works(client):
    a = org(client)
    inv = invite(client, a["id"], "carl@x.io").json()["invitation"]
    assert client.delete(f"/api/orgs/{a['id']}/invitations/{inv['id']}", headers=hdr("a@x.io", "bench-a")).status_code == 204
    assert not _revoke_events(client, a["id"])[0]["meta"].get("reason")
