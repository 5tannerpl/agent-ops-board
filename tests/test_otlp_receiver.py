import pytest

pytest.importorskip("opentelemetry.proto")

from fastapi.testclient import TestClient
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

import config
import demo_store
import otlp_store
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


def observation(number, name, attrs=None, parent=None, trace=None):
    req = ExportTraceServiceRequest.FromString(payload([(name, f"{number:016x}", parent)]))
    scope = req.resource_spans[0].scope_spans[0]
    scope.scope.name, scope.scope.version = "sample", "1"
    span = scope.spans[0]
    if trace:
        span.trace_id = bytes.fromhex(trace)
    span.kind = 1
    span.start_time_unix_nano = 1740000000000000001
    span.end_time_unix_nano = span.start_time_unix_nano + 2
    for key, value in (attrs or {}).items():
        arm = {str: "string_value", bool: "bool_value", int: "int_value"}[type(value)]
        span.attributes.add(key=key, value=AnyValue(**{arm: value}))
    return req


def post(client, req):
    return client.post("/v1/traces", content=req.SerializeToString(), headers=headers())


def summary():
    conn = demo_store.connect()
    try:
        return otlp_store.summary(conn, "local-agent", "agent-run-1")
    finally:
        conn.close()


def test_typed_evidence_retries_and_conflict(client):
    for i, (name, attrs) in enumerate([
        ("aob.worker.task", {"aob.attempt": 1, "aob.task.status": "FAILED"}),
        ("aob.unit.task", {"aob.attempt": 2, "aob.task.status": "COMPLETE", "aob.task_id": "job"}),
        ("result.validation", {"aob.validation.passed": True}),
        ("aob.result.validation", {"aob.validation.passed": False}),
        ("result.validation", {}),
        ("agent.classify", {}),
        ("chat model", {"gen_ai.usage.input_tokens": 0}),
    ], 1):
        req = observation(i, name, attrs)
        if i == 2:
            req.resource_spans[0].scope_spans[0].spans[0].attributes[2].value.string_value = "worker-2"
        assert post(client, req).status_code == 200
    s = summary()
    assert (s["completed_units"], s["failed_units"], s["unknown_outcome_units"], s["worker_count"]) == (1, 0, 0, 2)
    assert (s["validation_passed"], s["validation_failed"], s["validation_unknown"], s["llm_calls"]) == (1, 1, 1, 1)
    assert s["token_usage"]["input_tokens"] == 0 and s["token_usage"]["output_tokens"] is None
    conn = demo_store.connect()
    row = otlp_store.timeline(conn, "local-agent", "agent-run-1")[1]
    conn.close()
    assert (row["resource"], row["kind"], row["scope_name"], row["scope_version"]) == (
        {"deployment.environment.name": "local"}, 1, "sample", "1")
    assert row["attributes"]["aob.task_id"] == "job"
    assert row["duration_ms"] == .000002 and row["end_time_unix_nano"] == "1740000000000000003"
    assert post(client, observation(8, "aob.unit.failed", {"aob.attempt": 2})).status_code == 200
    assert summary()["unknown_outcome_units"] == 1
    conflict = observation(9, "new")
    conflict.resource_spans.extend(observation(1, "changed").resource_spans)
    assert post(client, conflict).status_code == 409
    assert summary()["span_count"] == 8 and summary()["identity_conflicts"] == 1


def test_boundaries_late_arrival_multitrace_and_cycle(client):
    external = "0000000000000010"
    trace = "0123456789abcdef0123456789abcdef"
    assert post(client, observation(2, "child", parent="0000000000000001")).is_success
    assert post(client, observation(1, "other", trace="ab"*16)).is_success
    manifest = {"run_id": "agent-run-1", "expected_spans": 3, "expected_units": 1, "external_parents": [
        {"trace_id": trace, "span_id": "0000000000000001", "parent_span_id": external}]}
    def complete(body):
        return client.post("/v1/traces/complete", headers=headers("application/json"), json=body)
    assert complete(manifest).json()["status"] == "PARTIAL"
    assert summary()["unresolved_parent_links"] == 1
    assert post(client, observation(1, "entry", parent=external)).is_success
    s = summary()
    assert (s["trace_status"], s["trace_count"], s["valid_parent_links"], s["external_parent_links"]) == (
        "TRACE_COMPLETE", 2, 1, 1)
    assert complete(manifest).json()["status"] == "TRACE_COMPLETE"
    assert complete(dict(manifest, expected_units=2)).status_code == 409
    assert post(client, observation(3, "cycle", parent="0000000000000003")).is_success
    assert summary()["trace_status"] == "PARTIAL" and summary()["cyclic_parent_links"] == 1


@pytest.mark.parametrize("attrs", [{"aob.attempt": True}, {"aob.attempt": 0}, {"aob.validation.passed": "false"},
                                   {"aob.task_id": 1}, {"aob.task.status": "ok"}])
def test_wrong_wire_types_rejected(client, attrs):
    assert post(client, observation(1, "test", attrs)).status_code == 422


def test_invalid_transport_and_manifest(client, monkeypatch):
    assert client.post("/v1/traces", content=b"\xff", headers=headers()).status_code == 400
    assert client.post("/v1/traces", content=b"", headers=headers()).status_code == 413
    req = observation(1, "test")
    span = req.resource_spans[0].scope_spans[0].spans[0]
    span.parent_span_id = b"\0"*8
    assert post(client, req).status_code == 422
    span.ClearField("parent_span_id")
    span.end_time_unix_nano = span.start_time_unix_nano - 1
    assert post(client, req).status_code == 422
    for invalid in (True, -1, "1", 1.5):
        assert client.post("/v1/traces/complete", headers=headers("application/json"), json={
            "run_id": "r", "expected_spans": invalid, "expected_units": 1}).status_code == 400
    monkeypatch.setattr(config, "OTLP_MAX_BYTES", 10)
    assert post(client, req).status_code == 413


def test_cross_run_rebinding_rejected(client):
    req = observation(1, "task")
    assert post(client, req).status_code == 200
    req.resource_spans[0].scope_spans[0].spans[0].attributes[0].value.string_value = "other-run"
    assert post(client, req).status_code == 409
    assert summary()["span_count"] == 1


def test_preserves_old_database(tmp_path):
    from storage import db
    conn = db.connect(tmp_path/"old.db")
    conn.executescript(otlp_store.SCHEMA)
    conn.execute("INSERT INTO external_agent_spans VALUES('s','local','r','u','w','t','id',NULL,"
                 "'result.validation',1,2,1000,'UNSET','valid','{}')")
    otlp_store.connect(conn)
    otlp_store.connect(conn)
    row = otlp_store.timeline(conn, "s", "r")[0]
    s = otlp_store.summary(conn, "s", "r")
    assert row["resource"] is row["start_time_unix_nano"] is row["environment"] is None
    assert s["unknown_outcome_units"] == s["validation_unknown"] == 1
    conn.close()
