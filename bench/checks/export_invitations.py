from tests.bench_common import hdr, invite, org


def test_export_lists_invitations_of_the_user(client):
    a = org(client)
    token = invite(client, a["id"], "bob@x.io", role="admin").json()["token"]
    assert client.post(f"/api/invitations/{token}/accept", headers=hdr("bob@x.io")).status_code == 200
    bob = next(m for m in client.get(f"/api/orgs/{a['id']}/members", headers=hdr("a@x.io", "bench-a")).json()
               if m["user"]["email"] == "bob@x.io")
    out = client.get(f"/api/users/{bob['user']['id']}/export", headers=hdr("a@x.io", "bench-a"))
    assert out.status_code == 200
    rows = out.json()["invitations"]
    assert len(rows) == 1
    assert rows[0]["org_id"] == a["id"] and rows[0]["role"] == "admin" and rows[0]["status"] == "accepted"
    assert "token" not in rows[0]  # an export never contains secrets
