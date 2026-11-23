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
RESOURCE_KEYS = {"aob.environment", "service.name", "service.version", "deployment.environment.name",
                 "service.instance.id", "cloud.provider", "cloud.platform", "cloud.region", "aob.code_commit"}
IDS = {"aob.run_id", "aob.unit_id", "aob.worker_id", "aob.task_id"}
INTS = {"aob.dataset_seed", "aob.attempt", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"}
SPAN_KEYS = IDS | INTS | {"aob.task.status", "aob.validation.passed", "aob.validation.result",
                          "aob.workflow", "aob.dataset_version", "aob.test_mode", "aob.environment",
                          "gen_ai.operation.name", "gen_ai.request.model",
                          "service.name", "service.version", "deployment.environment.name"}
SENSITIVE_ATTRIBUTE_PREFIXES = ("user.", "http.request.header.", "db.", "gen_ai.prompt")


def _value(value):
    kind = value.WhichOneof("value")
    return getattr(value, kind) if kind else None


def _attrs(items, allowed):
    result = {}
    for item in items:
        if item.key not in allowed:
            if item.key.startswith(SENSITIVE_ATTRIBUTE_PREFIXES):
                raise HTTPException(422, "sensitive attribute is not allowed")
            continue
        value = _value(item.value)
        key = item.key
        if key in INTS:
            valid = type(value) is int and (key == "aob.dataset_seed" or value >= int(key == "aob.attempt"))
        elif key == "aob.validation.passed":
            valid = type(value) is bool
        elif key == "aob.validation.result":
            valid = isinstance(value, (str, int, float, bool)) and len(str(value)) <= 256
        else:
            valid = isinstance(value, str) and bool(value.strip()) and len(value) <= (128 if key in IDS else 256)
        if not valid or key in result or (key == "aob.task.status" and value not in ("COMPLETE", "FAILED")):
            raise HTTPException(422, f"invalid or duplicate {key}")
        result[key] = value
    return result


def _source(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.isascii() or not header.lower().startswith("bearer "):
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
        resource = _attrs(resource_scope.resource.attributes, RESOURCE_KEYS)
        environment = resource.get("deployment.environment.name") or resource.get("aob.environment")
        for scope in resource_scope.scope_spans:
            for item in scope.spans:
                attrs = _attrs(item.attributes, SPAN_KEYS)
                if len(item.trace_id) != 16 or not any(item.trace_id):
                    raise HTTPException(422, "trace_id is invalid")
                if len(item.span_id) != 8 or not any(item.span_id):
                    raise HTTPException(422, "span_id is invalid")
                run_id = attrs.get("aob.run_id")
                if not isinstance(run_id, str) or not run_id:
                    raise HTTPException(422, "aob.run_id is required")
                if item.parent_span_id and (len(item.parent_span_id) != 8 or not any(item.parent_span_id)):
                    raise HTTPException(422, "parent_span_id is invalid")
                if any(len(s) > 256 for s in (item.name, scope.scope.name, scope.scope.version)):
                    raise HTTPException(422, "span or scope name/version exceeds limit")
                start, end = item.start_time_unix_nano, item.end_time_unix_nano
                if not start or end < start:
                    raise HTTPException(422, "invalid span timestamps")
                parent = item.parent_span_id.hex() or None
                if item.status.code not in (0, 1, 2):
                    raise HTTPException(422, "invalid status code")
                status = ("UNSET", "OK", "ERROR")[item.status.code]
                spans.append({
                    "source_id": source_id, "environment": environment or "", "run_id": run_id,
                    "unit_id": attrs.get("aob.unit_id"), "worker_id": attrs.get("aob.worker_id"),
                    "trace_id": item.trace_id.hex(), "span_id": item.span_id.hex(),
                    "parent_span_id": parent, "span_name": item.name, "start_time": start / 1e9, "end_time": end / 1e9,
                    "start_time_unix_nano": str(start), "end_time_unix_nano": str(end),
                    "resource": resource, "kind": item.kind, "scope_name": scope.scope.name,
                    "scope_version": scope.scope.version, "duration_ms": (end - start) / 1e6, "status": status,
                    "validation_result": str(attrs.get("aob.validation.result", "valid")),
                    "attributes": attrs,
                })
    if len(spans) > config.OTLP_MAX_SPANS:
        raise HTTPException(413, "span limit exceeded")
    return spans


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > config.OTLP_MAX_BYTES:
            raise HTTPException(413, "OTLP payload too large")
        body.extend(chunk)
    return bytes(body)


@router.post("/v1/traces")
async def receive_otlp(request: Request):
    source_id = _source(request)
    if request.headers.get("content-type", "").lower() != "application/x-protobuf":
        raise HTTPException(415, "content type must be application/x-protobuf")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > config.OTLP_MAX_BYTES:
        raise HTTPException(413, "OTLP payload too large")
    body = await _bounded_body(request)
    if not body or len(body) > config.OTLP_MAX_BYTES:
        raise HTTPException(413, "OTLP payload too large")
    spans = _decode(body, source_id)
    if not spans:
        raise HTTPException(422, "OTLP payload contains no spans")
    conn = demo_store.connect()
    try:
        accepted, duplicate = otlp_store.insert_spans(conn, spans)
    except otlp_store.IdentityConflict as exc:
        raise HTTPException(409, "conflicting span identity") from exc
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
        body = json.loads(await _bounded_body(request))
        run_id = body["run_id"]
        expected_spans = body["expected_spans"]
        expected_units = body["expected_units"]
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128 or not all(
                type(n) is int and 0 <= n < 2**63 for n in (expected_spans, expected_units)):
            raise ValueError
        boundaries = otlp_store.parse_boundaries(body.get("external_parents", []))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid completion payload") from exc
    conn = demo_store.connect()
    try:
        return otlp_store.complete(conn, source_id, run_id, expected_spans, expected_units, boundaries)
    except otlp_store.ManifestConflict as exc:
        raise HTTPException(409, "conflicting completion manifest") from exc
    finally:
        conn.close()
