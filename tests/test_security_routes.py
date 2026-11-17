from fastapi.testclient import TestClient

from api.private_app import app as private_app
from api.public_ingress import app as ingress_app


def test_public_ingress_exposes_only_event_endpoint():
    client = TestClient(ingress_app)
    assert client.get("/api/demo/runs").status_code == 404
    assert client.get("/v1/dashboard").status_code == 404
    assert client.get("/docs").status_code == 404


def test_private_api_does_not_expose_event_ingress():
    client = TestClient(private_app)
    assert client.post("/api/events", json={}).status_code == 404
