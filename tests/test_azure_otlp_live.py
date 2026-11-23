"""Opt-in cross-check with the sibling sample's own locked Python environment."""
import json
import os
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

import config
from api.private_app import app as private
from api.public_ingress import app as ingress
from tests.test_demo_api import auth, demo


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline, "loopback startup failed"
                time.sleep(.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


RUN = """
import json, os
from fastapi.testclient import TestClient
from app.main import create_app
headers = {"Authorization": "Bearer " + os.environ["RUN_API_TOKEN"]}
if os.environ["AOB_OTLP_FINALIZE"] == "1":
    headers["traceparent"] = "00-" + "aa"*16 + "-" + "bb"*8 + "-01"
with TestClient(create_app()) as client:
    r = client.post("/demo/runs", json={"external_run_id": os.environ["RUN_ID"], "task_count": 100}, headers=headers)
    r.raise_for_status()
    print(json.dumps(r.json()))
"""


@pytest.mark.parametrize("remote_parent", [False, True])
def test_actual_azure_export_and_private_read(demo, monkeypatch, remote_parent):
    python = os.getenv("AOB_AZURE_TEST_PYTHON")
    if not python:
        pytest.skip("set AOB_AZURE_TEST_PYTHON to sample's locked test interpreter")
    sample = Path(__file__).resolve().parents[2] / "aob-cloud-connectors/samples/azure-python-agent"
    source, credential = "local-python-agent", secrets.token_urlsafe(32)
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", json.dumps({source: credential}))
    batches = []

    async def record(scope, receive, send):
        if scope.get("path") != "/v1/traces":
            return await ingress(scope, receive, send)
        body = bytearray()
        async def tee():
            message = await receive()
            body.extend(message.get("body", b""))
            return message
        await ingress(scope, tee, send)
        batches.append(bytes(body))

    run_id = "azure-py-" + str(uuid.uuid4())
    with serve(record) as public, serve(private) as reader, httpx.Client(trust_env=False, timeout=10) as client:
        env = {"PATH": os.environ["PATH"], "AOB_EXPORTER": "otlp", "RUN_API_TOKEN": secrets.token_urlsafe(32),
               "OTLP_SOURCE_TOKEN": credential, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": public + "/v1/traces",
               "AOB_OTLP_FINALIZE": str(int(remote_parent)), "RUN_ID": run_id}
        result = subprocess.run([python, "-c", RUN], env=env, cwd=sample, capture_output=True, text=True,
                                timeout=90, check=True)
        run = json.loads(result.stdout)
        assert (run["status"], run["completed"], run["failed"]) == ("COMPLETE", 100, 0)
        assert run["telemetry_delivery"] == {"exporter": "otlp", "emitted_spans": 405, "successful_batch_spans": 405,
                                             "failed_batches": 0, "flush_completed": True}
        assert run["telemetry_delivery_failed"] is False
        read_auth = auth()
        ingest = {"Authorization": "Bearer " + credential}
        base = reader + "/api/external/runs/" + run_id
        def read(path):
            r = client.get(base + path, headers=read_auth)
            r.raise_for_status()
            return r.json()
        if not remote_parent:
            assert run["trace_finalization"] is None and read("/summary")["trace_status"] == "PARTIAL"
            manifest = {"run_id": run_id, "expected_spans": 405, "expected_units": 100}
            assert client.post(public + "/v1/traces/complete", headers=ingest, json=manifest).json()["status"] == "TRACE_COMPLETE"
        else:
            assert run["trace_finalization"] == "TRACE_COMPLETE"
        s = read("/summary")
        assert (s["trace_count"], s["unique_units"], s["worker_count"], s["completed_units"], s["failed_units"],
                s["unknown_outcome_units"], s["validation_passed"], s["validation_failed"], s["validation_unknown"],
                s["valid_parent_links"], s["external_parent_links"], s["unresolved_parent_links"], s["llm_calls"]) == (
                    1, 100, 4, 100, 0, 0, 100, 0, 0, 404, int(remote_parent), 0, 0)
        assert s["environment"] is None and s["trace_status"] == "TRACE_COMPLETE"
        assert [w["unit_count"] for w in read("/workers")["items"]] == [25]*4
        trace = read("/traces")["items"][0]
        assert trace["trace_id"] == run["trace_id"] and trace["span_count"] == 405
        rows = read("/traces/" + trace["trace_id"] + "/timeline")["items"]
        ids = {r["span_id"]: r for r in rows}
        assert len(ids) == len(rows) == 405
        counts = Counter()
        children = Counter()
        for row in rows:
            role = row["role"]
            counts[row["span_name"]] += 1
            if role == "run":
                assert row["parent_span_id"] == ("bb"*8 if remote_parent else None)
                continue
            parent = ids[row["parent_span_id"]]
            children[parent["span_id"], role] += 1
            if role == "worker":
                assert parent["role"] == "run"
            elif role == "unit_execution":
                assert parent["role"] == "worker" and parent["worker_id"] == row["worker_id"]
                assert row["attributes"]["aob.task.status"] == "COMPLETE"
            else:
                assert parent["role"] == "unit_execution" and parent["unit_id"] == row["unit_id"]
                assert parent["worker_id"] == row["worker_id"]
        assert counts == {"aob.azure.run": 1, "aob.azure.worker": 4, "aob.unit.task": 100,
                          "agent.classify": 100, "tool.lookup": 100, "result.validation": 100}
        for row in rows:
            if row["role"] == "unit_execution":
                assert all(children[row["span_id"], r] == 1 for r in ("classify", "lookup", "validate"))
        assert client.get(base + "/summary", headers=ingest).status_code == 401
        subprocess.run([python, "verify_aob.py", run_id], cwd=sample, timeout=15, check=True,
                       env={"AOB_READ_BASE_URL": reader, "AOB_READ_AUTH_HEADER": "Authorization",
                            "AOB_READ_AUTH_VALUE": read_auth["Authorization"]})
        wire = dict(ingest, **{"Content-Type": "application/x-protobuf"})
        duplicates = 0
        for data in list(batches):
            ack = client.post(public + "/v1/traces", content=data, headers=wire)
            assert ack.status_code == 200 and ack.headers["x-aob-accepted-spans"] == "0"
            duplicates += int(ack.headers["x-aob-duplicate-spans"])
        assert duplicates == 405 and read("/summary")["span_count"] == 405
        changed = ExportTraceServiceRequest.FromString(batches[0])
        changed.resource_spans[0].scope_spans[0].spans[0].name = "changed"
        assert client.post(public + "/v1/traces", content=changed.SerializeToString(), headers=wire).status_code == 409
        assert read("/summary")["trace_status"] == "PARTIAL"
        print(f"{run_id} trace={trace['trace_id']} producer=COMPLETE acknowledged=405 persisted=405 "
              f"internal=404 external={int(remote_parent)} manifest=TRACE_COMPLETE duplicates={duplicates} conflict=409")
