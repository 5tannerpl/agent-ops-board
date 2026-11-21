import pytest

pytest.importorskip("opentelemetry.proto")

from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

import config
from api.public_ingress import app


def payload(spans):
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    resource_spans.resource.attributes.add(
        key="deployment.environment.name", value=AnyValue(string_value="local")
    )
    scope_spans = resource_spans.scope_spans.add()
    for index, (span_name, span_id, parent_id) in enumerate(spans):
        span = scope_spans.spans.add(name=span_name)
        span.trace_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
        span.span_id = bytes.fromhex(span_id)
        if parent_id:
            span.parent_span_id = bytes.fromhex(parent_id)
        span.start_time_unix_nano = (index + 1) * 1_000_000_000
        span.end_time_unix_nano = (index + 2) * 1_000_000_000
        span.attributes.add(key="aob.run_id", value=AnyValue(string_value="agent-run-1"))
        span.attributes.add(key="aob.unit_id", value=AnyValue(string_value=f"unit-{index + 1}"))
        span.attributes.add(key="aob.worker_id", value=AnyValue(string_value="worker-1"))
    return request.SerializeToString()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEMO_DB_PATH", tmp_path / "demo.db")
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", '{"local-agent":"secret"}')
    with TestClient(app) as value:
        yield value


def headers(content_type="application/x-protobuf", token="secret"):
    return {"Authorization": f"Bearer {token}", "Content-Type": content_type}


def test_valid_otlp_and_duplicate_are_idempotent(client):
    body = payload([("aob.unit.completed", "0000000000000001", None)])
    first = client.post("/v1/traces", content=body, headers=headers())
    second = client.post("/v1/traces", content=body, headers=headers())
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/x-protobuf"
    assert first.headers["x-aob-accepted-spans"] == "1"
    assert second.headers["x-aob-duplicate-spans"] == "1"


def test_authentication_and_content_type_are_strict(client):
    body = payload([("aob.unit.completed", "0000000000000002", None)])
    assert client.post("/v1/traces", content=body, headers=headers(token="wrong")).status_code == 401
    assert client.post("/v1/traces", content=body, headers=headers("application/json")).status_code == 415


def test_out_of_order_parent_is_reconciled(client):
    body = payload([
        ("aob.child", "0000000000000003", "0000000000000004"),
        ("aob.parent", "0000000000000004", None),
    ])
    response = client.post("/v1/traces", content=body, headers=headers())
    assert response.status_code == 200
    completion = client.post(
        "/v1/traces/complete", json={"run_id": "agent-run-1", "expected_spans": 2, "expected_units": 2},
        headers=headers("application/json"),
    )
    assert completion.json()["status"] == "TRACE_COMPLETE"


def test_sensitive_attributes_are_rejected(client):
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add(name="unsafe")
    span.trace_id = bytes.fromhex("0123456789abcdef0123456789abcdef")
    span.span_id = bytes.fromhex("0000000000000005")
    span.start_time_unix_nano = 1_000_000_000
    span.end_time_unix_nano = 2_000_000_000
    span.attributes.add(key="user.email", value=AnyValue(string_value="secret@example.test"))
    assert client.post("/v1/traces", content=request.SerializeToString(), headers=headers()).status_code == 422
