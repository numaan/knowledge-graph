"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """The SSRF guard of a run resolves the source host. Tests must not use the network: every host is public."""
    from kg_pipeline.control import probe
    monkeypatch.setattr(probe, "resolve", lambda host: ["93.184.216.34"])
