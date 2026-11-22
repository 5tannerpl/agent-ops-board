import json

import pytest

import config
from kaggle_launcher import LaunchError, render_prelude
from security.production_config import validate_production_config


@pytest.fixture
def staging_config(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed_users.json"
    allowed.write_text(json.dumps({"allowed_users": {}}), encoding="utf-8")
    event_secret = tmp_path / "event-secret"
    event_secret.write_text("local-staging-only-secret\n", encoding="utf-8")
    monkeypatch.setattr(config, "AOB_ENV", "staging")
    monkeypatch.setattr(config, "AUTH0_DOMAIN", "tenant.example.auth0.com")
    monkeypatch.setattr(config, "AUTH0_AUDIENCE", "https://agent-ops-board-api")
    monkeypatch.setattr(config, "ALLOWED_USERS_FILE", allowed)
    monkeypatch.setattr(config, "EVENT_SECRET_FILE", event_secret)
    monkeypatch.setattr(config, "EVENT_SECRET", "")
    monkeypatch.setattr(config, "EVENTS_PUBLIC_URL", config.STAGING_EVENTS_URL)


def test_staging_requires_all_authentication_inputs(staging_config):
    validate_production_config()


def test_staging_launcher_uses_only_internal_event_service(staging_config):
    prelude = render_prelude("run-" + "a" * 16)
    assert "http://event-ingress:8011/api/events" in prelude
    assert "http://event-ingress:8011/api/traces" in prelude


def test_staging_launcher_rejects_external_http(staging_config, monkeypatch):
    monkeypatch.setattr(config, "EVENTS_PUBLIC_URL", "http://example.test/api/events")
    with pytest.raises(LaunchError, match="isolated event-ingress"):
        render_prelude("run-" + "a" * 16)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:18011/api/events",
        "http://events.marcheagents.com/api/events",
        "http://event-ingress:8011/api/events?unsafe=1",
        "https://event-ingress:8011/api/events",
    ],
)
def test_staging_rejects_non_isolated_event_urls(staging_config, monkeypatch, url):
    monkeypatch.setattr(config, "EVENTS_PUBLIC_URL", url)
    with pytest.raises(RuntimeError, match="AOB_EVENTS_URL=http://event-ingress:8011/api/events"):
        validate_production_config()


@pytest.mark.parametrize("attribute", ["AUTH0_DOMAIN", "AUTH0_AUDIENCE", "EVENT_SECRET"])
def test_staging_does_not_bypass_required_credentials(staging_config, tmp_path, monkeypatch, attribute):
    if attribute == "EVENT_SECRET":
        monkeypatch.setattr(config, "EVENT_SECRET_FILE", tmp_path / "missing-secret")
    else:
        monkeypatch.setattr(config, attribute, "")
    with pytest.raises(RuntimeError, match=attribute if attribute != "EVENT_SECRET" else "AOB_EVENT_SECRET_FILE"):
        validate_production_config()
