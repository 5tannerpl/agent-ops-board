"""Standard OTLP/HTTP protobuf receiver for independently running agents."""
from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, HTTPException, Request, Response
from google.protobuf.message import DecodeError

import config
import demo_store
import otlp_store

router = APIRouter()
ALLOWED_ATTRIBUTES = {
    "aob.run_id", "aob.unit_id", "aob.worker_id", "aob.environment", "aob.validation.result",
    "service.name", "service.version", "deployment.environment.name",
}
SENSITIVE_ATTRIBUTE_PREFIXES = ("user.", "http.request.header.", "db.", "gen_ai.prompt")


def _value(value):
    kind = value.WhichOneof("value")
    return getattr(value, kind) if kind else None


def _attrs(items):
    result = {}
    for item in items:
        if item.key not in ALLOWED_ATTRIBUTES:
            if item.key.startswith(SENSITIVE_ATTRIBUTE_PREFIXES):
                raise HTTPException(422, "sensitive attribute is not allowed")
            continue
        value = _value(item.value)
        if isinstance(value, (str, bool, int, float)) and len(str(value)) <= 256:
            result[item.key] = value
    return result


def _source(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Bearer token required")
    token = header[7:].strip()
    for source_id, expected in config.get_otlp_source_tokens().items():
        if hmac.compare_digest(token, expected):
            return source_id
    raise HTTPException(401, "invalid OTLP source token")


def _decode(payload: bytes, source_id: str) -> list[dict]:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    message = ExportTraceServiceRequest()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise HTTPException(400, "invalid OTLP protobuf") from exc
    spans = []
    for resource_scope in message.resource_spans:
        resource = _attrs(resource_scope.resource.attributes)
        environment = resource.get("deployment.environment.name") or resource.get("aob.environment") or "local"
        for scope in resource_scope.scope_spans:
            for item in scope.spans:
                attrs = _attrs(item.attributes)
                if len(item.trace_id) != 16 or not any(item.trace_id):
                    raise HTTPException(422, "trace_id is invalid")
                if len(item.span_id) != 8 or not any(item.span_id):
                    raise HTTPException(422, "span_id is invalid")
                run_id = attrs.get("aob.run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise HTTPException(422, "aob.run_id is required")
                start = item.start_time_unix_nano / 1_000_000_000
                end = item.end_time_unix_nano / 1_000_000_000
                if not start or end < start:
                    raise HTTPException(422, "invalid span timestamps")
                parent = item.parent_span_id.hex() or None
                status = {0: "UNSET", 1: "OK", 2: "ERROR"}.get(item.status.code, "UNSET")
                spans.append({
                    "source_id": source_id, "environment": str(environment), "run_id": run_id,
                    "unit_id": attrs.get("aob.unit_id"), "worker_id": attrs.get("aob.worker_id"),
                    "trace_id": item.trace_id.hex(), "span_id": item.span_id.hex(),
                    "parent_span_id": parent, "span_name": item.name, "start_time": start, "end_time": end,
                    "duration_ms": (end - start) * 1000, "status": status,
                    "validation_result": str(attrs.get("aob.validation.result", "valid")),
                    "attributes": attrs,
                })
    if len(spans) > config.OTLP_MAX_SPANS:
        raise HTTPException(413, "span limit exceeded")
    return spans


@router.post("/v1/traces")
async def receive_otlp(request: Request):
    source_id = _source(request)
    if request.headers.get("content-type", "").lower() != "application/x-protobuf":
        raise HTTPException(415, "content type must be application/x-protobuf")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.OTLP_MAX_BYTES:
        raise HTTPException(413, "OTLP payload too large")
    body = await request.body()
    if not body or len(body) > config.OTLP_MAX_BYTES:
        raise HTTPException(413, "OTLP payload too large")
    spans = _decode(body, source_id)
    if not spans:
        raise HTTPException(422, "OTLP payload contains no spans")
    conn = demo_store.connect()
    try:
        otlp_store.connect(conn)
        accepted, duplicate = otlp_store.insert_spans(conn, spans)
    finally:
        conn.close()
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

    response = ExportTraceServiceResponse()
    return Response(
        content=response.SerializeToString(),
        media_type="application/x-protobuf",
        headers={"X-AOB-Accepted-Spans": str(accepted), "X-AOB-Duplicate-Spans": str(duplicate)},
    )


@router.post("/v1/traces/complete")
async def complete_otlp(request: Request):
    source_id = _source(request)
    if request.headers.get("content-type", "").lower() != "application/json":
        raise HTTPException(415, "content type must be application/json")
    try:
        body = json.loads(await request.body())
        run_id = body["run_id"]
        expected_spans = int(body["expected_spans"])
        expected_units = int(body["expected_units"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid completion payload") from exc
    conn = demo_store.connect()
    try:
        otlp_store.connect(conn)
        return otlp_store.complete(conn, source_id, run_id, expected_spans, expected_units)
    finally:
        conn.close()
