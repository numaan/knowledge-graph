from datetime import datetime, timedelta, timezone
from uuid import UUID

from tests.bench_common import invite, org


def _expires(client, inv_id):
    from app.models.tables import Invitation
    with client.testing_session() as db:
        return db.get(Invitation, UUID(inv_id)).expires_at


def test_default_lifetime_is_still_seven_days(client):
    a = org(client)
    inv = invite(client, a["id"], "bob@x.io").json()["invitation"]
    left = _expires(client, inv["id"]) - datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(days=6, hours=23) < left <= timedelta(days=7)


def test_lifetime_follows_the_setting(client, monkeypatch):
    from app.config import get_settings
    assert get_settings().invite_ttl_days == 7
    monkeypatch.setattr(get_settings(), "invite_ttl_days", 2)
    a = org(client)
    inv = invite(client, a["id"], "dan@x.io").json()["invitation"]
    left = _expires(client, inv["id"]) - datetime.now(timezone.utc).replace(tzinfo=None)
    assert timedelta(days=1, hours=23) < left <= timedelta(days=2)
