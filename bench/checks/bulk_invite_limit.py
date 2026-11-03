from tests.bench_common import hdr, org


def _bulk(client, org_id, n):
    rows = [{"email": f"u{i}@x.io", "role": "member"} for i in range(n)]
    return client.post(f"/api/orgs/{org_id}/invitations/bulk", json={"invites": rows}, headers=hdr("a@x.io", "bench-a"))


class _NoEmail:
    available = True

    def send(self, *args):
        pass


def test_bulk_invite_accepts_50_rows(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "ratelimit_enabled", False)
    monkeypatch.setattr("app.routers.orgs.get_email_provider", lambda settings: _NoEmail())  # no SMTP per row
    a = org(client)
    r = _bulk(client, a["id"], 50)
    assert r.status_code == 200 and r.json()["created"] == 50


def test_bulk_invite_rejects_51_rows(client):
    a = org(client)
    assert _bulk(client, a["id"], 51).status_code == 422
