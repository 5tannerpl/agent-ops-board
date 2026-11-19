import json

import pytest
from fastapi.testclient import TestClient

import config
from api.private_app import app as private_app
from api.public_ingress import app as ingress_app


@pytest.fixture
def production_config(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed_users.json"
    allowed.write_text(json.dumps({"allowed_users": {}}), encoding="utf-8")
    event_secret = tmp_path / "event-secret"
    event_secret.write_text("test-production-secret\n", encoding="utf-8")
    monkeypatch.setattr(config, "AOB_ENV", "production")
    monkeypatch.setattr(config, "AUTH0_DOMAIN", "tenant.example.auth0.com")
    monkeypatch.setattr(config, "AUTH0_AUDIENCE", "https://agent-ops-board-api")
    monkeypatch.setattr(config, "ALLOWED_USERS_FILE", allowed)
    monkeypatch.setattr(config, "EVENT_SECRET_FILE", event_secret)
    monkeypatch.setattr(config, "EVENT_SECRET", "")
    monkeypatch.setattr(config, "EVENTS_PUBLIC_URL", "https://demo.example.com/api/events")
    monkeypatch.setattr(config, "DEMO_DB_PATH", tmp_path / "demo.db")


def test_production_apps_start_with_file_secrets(production_config):
    with TestClient(private_app) as private_client:
        assert private_client.get("/healthz").json() == {"status": "ok"}
    with TestClient(ingress_app) as ingress_client:
        assert ingress_client.get("/healthz").json() == {"status": "ok"}


def test_production_startup_requires_event_secret_file(production_config, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EVENT_SECRET_FILE", tmp_path / "missing-secret")
    with pytest.raises(RuntimeError, match="AOB_EVENT_SECRET_FILE"):
        with TestClient(private_app):
            pass
