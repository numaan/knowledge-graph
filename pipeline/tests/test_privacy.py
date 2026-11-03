import pytest

from kg_pipeline import privacy
from kg_pipeline.privacy import kinds, mask, protect_row, redact

# Test values are built at run time so that secret scanners do not flag this file.
GH = "ghp_" + "a1B2c3D4e5" * 4
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0." + "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7\nabc\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize("text,kind", [
    (f"token {GH} in the log", "github_token"),
    (f"aws_access_key_id = {AWS}", "aws_access_key"),
    (f"Authorization: Bearer {'Zm9v' * 8}", "bearer_token"),
    (f"cookie {JWT}", "jwt"),
    (KEY, "private_key"),
    ("DATABASE_URL=postgres://app:s3cr3tPass@db.internal:5432/app", "url_password"),
    ('db_password = "hunter2hunter"', "password"),
    ('{"client_secret": "a8f5f167f44f4964e6c998dee827110c"}', "password"),
    ("spring.datasource.password: Summer2026!", "password"),
    ("xoxb-" + "1234567890-abcdefghij", "slack_token"),
    ("AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY", "google_api_key"),
    ("sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "stripe_key"),
])
def test_secrets_are_found_and_never_kept(text, kind):
    assert kind in kinds(text)
    out = redact(text)
    assert f"[SECRET:{kind}]" in out
    assert not {"hunter2hunter", "s3cr3tPass", "Summer2026!", GH, AWS, JWT, "MIIEowIBAAKCAQEA7"} & set(out.split())
    assert "s3cr3tPass" not in out and "MIIEowIBAAKCAQEA7" not in out


@pytest.mark.parametrize("text,kind", [
    ("Contact jane.doe@acme.io for access", "email"),
    ("Call +44 20 7946 0958 or (555) 123-4567", "phone"),
    ("card 4111 1111 1111 1111 on file", "card"),
    ("IBAN GB82 WEST 1234 5698 7654 32", "iban"),
    ("SSN 078-05-1120", "ssn"),
])
def test_pii_is_masked_but_kept_in_stored_text(text, kind):
    assert kind in kinds(text)
    assert f"[{kind.upper()}]" in mask(text)
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    "Write to bot@example.com or dev@test",                    # example domains
    "git clone git@github.com:acme/app.git",
    "password: ${DB_PASSWORD}",                                # placeholders
    'api_key = "<your-api-key>"',
    "password = os.environ['DB_PASSWORD']",
    "if password == other: return",                            # code, not a value
    "Reset your password: open the settings page",
    "Release 2026-09-22, build 1.23.1, host 192.168.100.200",  # dates, versions, addresses
    "order 4111111111111112 is not a valid card",              # fails the Luhn check
    "the [SECRET:github_token] was revoked",                   # already masked
])
def test_allow_list_and_false_positives(text):
    assert mask(text) == text, kinds(text)


def test_extra_allow_list_and_mode_off(monkeypatch):
    monkeypatch.setenv("KG_PII_ALLOW", "ops@acme.io,re:^build-.*@acme\\.io$")
    assert mask("ops@acme.io build-7@acme.io jane@acme.io") == "ops@acme.io build-7@acme.io [EMAIL]"
    monkeypatch.setenv("KG_PII_MODE", "off")
    assert mask(f"jane@acme.io {GH}") == f"jane@acme.io {GH}" and protect_row({"text": GH}) == {"text": GH}
    monkeypatch.setenv("KG_PII_MODE", "loud")
    with pytest.raises(ValueError):
        privacy.mode()


def test_protect_row_stores_masked_text_and_kinds():
    row = protect_row({"id": "jira:X-1", "title": f"Leak {GH}", "text": f"Ask jane@acme.io; token {GH}", "acl": ["jira:X"]})
    assert row["title"] == "Leak [SECRET:github_token]"
    assert row["text"] == "Ask jane@acme.io; token [SECRET:github_token]"
    assert row["text_masked"] == "Ask [EMAIL]; token [SECRET:github_token]"
    assert row["pii"] == ["email", "github_token"]
    clean = protect_row({"id": "x", "text": "nothing to hide here"})
    assert "text_masked" not in clean and "pii" not in clean


def test_llm_and_embedding_inputs_are_masked(monkeypatch):
    from kg_pipeline import models
    sent = []
    monkeypatch.setattr(models, "_post", lambda url, body, *a, **k: sent.append(body) or {"message": {"content": "{}"}})
    models.LLM().json("system", f"user jane@acme.io {GH}", {})
    assert "jane@acme.io" not in str(sent) and GH not in str(sent) and "[EMAIL]" in str(sent)
