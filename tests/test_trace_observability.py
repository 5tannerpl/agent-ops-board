import json
import time

from demo_auth import sign_event

from tests.test_demo_api import auth, demo


def _span(name, span_id, parent_id=None, **attributes):
    return {
        "trace_id": "a" * 32,
        "span_id": span_id,
        "parent_id": parent_id,
        "name": name,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "status": "OK",
        "attributes": {"aob.run_id": "run-placeholder", **attributes},
        "resource": {"attributes": {"service.name": "test"}},
    }


def _post_trace(client, run_id, spans, batch_id="batch-1", final=True):
    payload = {
        "run_id": run_id, "batch_id": batch_id, "schema_version": 1,
        "final": final, "spans": spans,
    }
    payload["spans"] = [{**span, "attributes": {k: (run_id if k == "aob.run_id" else v)
                                                 for k, v in span["attributes"].items()}}
                       for span in spans]
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    return client.ingress.post(
        "/api/traces", content=body,
        headers={"X-AOB-Timestamp": timestamp, "X-AOB-Signature": sign_event(run_id, timestamp, body),
                 "Content-Type": "application/json"},
    )


def test_trace_ingestion_is_signed_bounded_and_idempotent(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    task = _span("aob.worker.task", "1" * 16, **{"aob.unit_id": "unit-1", "aob.worker_id": "worker-1",
                                                  "aob.attempt": 1})
    llm = _span("chat test-model", "2" * 16, "1" * 16, **{"gen_ai.operation.name": "chat",
                                                           "gen_ai.request.model": "test-model",
                                                           "gen_ai.usage.input_tokens": 10,
                                                           "gen_ai.usage.output_tokens": 4})
    validation = _span("aob.result.validation", "3" * 16, "1" * 16)
    assert _post_trace(demo, run_id, [task, llm, validation]).status_code == 202
    assert _post_trace(demo, run_id, [task, llm, validation]).json()["status"] == "duplicate"
    summary = demo.get(f"/api/demo/{run_id}/trace-summary", headers=auth()).json()
    assert summary["llm_calls"] == 1
    assert summary["unique_units"] == 1
    assert summary["input_tokens"] == 10 and summary["output_tokens"] == 4
    calls = demo.get(f"/api/demo/{run_id}/llm-calls", headers=auth()).json()["items"]
    assert calls[0]["attributes"]["aob.unit_id"] == "unit-1"
    assert demo.get(f"/api/demo/{run_id}/units/unit-1/trace", headers=auth()).json()["spans"]


def test_trace_rejects_bad_signature_and_sensitive_attributes(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    span = _span("chat test-model", "4" * 16, prompt="must-not-be-uploaded")
    response = _post_trace(demo, run_id, [span])
    assert response.status_code == 422
    assert demo.ingress.post("/api/traces", json={"run_id": run_id}).status_code == 401


def test_trace_queries_require_run_owner(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    assert demo.get(f"/api/demo/{run_id}/trace-summary").status_code == 401
    assert demo.get(f"/api/demo/{run_id}/trace-summary", headers=auth("auth0|bob")).status_code == 404
