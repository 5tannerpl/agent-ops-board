import json
import os
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import config
import demo_auth
import demo_service
import demo_store
import otlp_store
from api.private_app import create_app
from api.public_ingress import app as ingress
from crosscloud import store
from crosscloud.commands import BindingSpec, RunCommandService
from crosscloud.policy import Principal, ServiceError
from crosscloud.queries import RunQueryService
from crosscloud.registry import DeploymentRegistryService, DeploymentSpec
from test_demo_api import DOMAIN, AUDIENCE, KEY, auth


REG = DeploymentRegistryService()
CMD = RunCommandService()
QUERY = RunQueryService()
SCOPES = "aob:deployments:read aob:deployments:write aob:runs:read aob:runs:write aob:runs:execute aob:metrics:read aob:traces:read"
ALICE = Principal("alice", frozenset({"east", "west"}), frozenset(SCOPES.split()))
BOB = replace(ALICE, owner="bob")
URL = "/api/v1/deployments"


def forbidden(*args, **kwargs):
    pytest.fail("unexpected network, provider or reconciliation call")


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEMO_DB_PATH", tmp_path / "demo.db")
    with closing(demo_store.connect()) as conn:
        historical = demo_store.create_run(conn, "alice")
        conn.executescript(store.SCHEMA)
        yield conn, historical["run_id"]


def test_registry_private_only_and_opt_in(tmp_path, monkeypatch):
    assert not config.CROSS_CLOUD_ENABLED
    assert TestClient(create_app()).get(URL).status_code == 404
    monkeypatch.setattr(config, "CROSS_CLOUD_ENABLED", True)
    monkeypatch.setattr(config, "DEMO_DB_PATH", tmp_path / "demo.db")
    monkeypatch.setattr(config, "OTLP_READ_SOURCES", '["east","west"]')
    monkeypatch.setattr(config, "AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setattr(config, "AUTH0_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(demo_auth, "signing_key", lambda token: KEY.public_key())
    policy = {"enabled": True, "external_sources": ["east"], "cross_cloud_scopes": SCOPES.split()}
    monkeypatch.setattr(demo_auth, "load_allowed_users", lambda: {"alice": policy, "bob": policy})
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    spec = {"provider": "private", "connection_mode": "private_connector", "runtime_type": "agent",
            "endpoint_ref": "ref:lan-agent", "source_id": "east"}
    headers = dict(auth("alice", scope=SCOPES), **{"Idempotency-Key": "alpha"})
    assert TestClient(ingress).post(URL, json=spec, headers=headers).status_code == 404
    with TestClient(create_app()) as api:
        for identity, expected in (({}, 401), (auth("alice", exp=1), 401), (auth("alice", aud="other"), 401),
                                   (auth("stranger", scope=SCOPES), 403), (auth("alice"), 403)):
            assert api.post(URL, json=spec, headers={**identity, "Idempotency-Key": "x"}).status_code == expected
        response = api.post(URL, json=spec, headers=headers)
        assert response.status_code == 201
        record = response.json()
        assert record["ownership"] == "external" and record["execution_policy"] == "observe_only"
        assert api.post(URL, json=spec, headers=headers).json() == record
        path = f"{URL}/{record['deployment_id']}"
        assert api.post(path + "/validate", headers=headers).json()["reachability"] == "unknown"
        assert api.get(path, headers=auth("bob", scope=SCOPES)).status_code == 404
        assert api.post(URL, json={**spec, "source_id": "west"}, headers=headers).status_code == 404
        for bad in ({"credential_ref": "raw-secret"}, {"endpoint_ref": "https://x/?key=raw-secret"},
                    {"owner_id": "bob"}, {"prompt": "raw-secret"}):
            rejected = api.post(URL, json={**spec, **bad}, headers=headers)
            assert rejected.status_code == 422 and "raw-secret" not in rejected.text
        assert api.get(URL + "?limit=101", headers=headers).status_code == 422
        policy["external_sources"] = []
        assert api.get(URL, headers=headers).json() == {"items": []}
        assert api.get(path, headers=headers).status_code == 404
    with closing(store.reader()) as conn:
        assert "raw-secret" not in "\n".join(conn.iterdump())


def test_idempotent_concurrent_replay_across_interpreter_restart(database):
    spec = DeploymentSpec(provider="custom", runtime_type="inference",
                          connection_mode="public_https", endpoint_ref="ref:existing-llm")

    def register(_):
        with closing(store.writer()) as conn:
            return REG.register(conn, ALICE, spec, "one")

    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(register, range(16)))
    assert all(record == records[0] for record in records)
    conn, _ = database
    assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0] == 1
    with pytest.raises(ServiceError, match="IDEMPOTENCY_CONFLICT"):
        REG.register(conn, ALICE, spec.model_copy(update={"provider": "other"}), "one")
    assert REG.register(conn, BOB, spec, "one")["deployment_id"] != records[0]["deployment_id"]
    program = """
import sys
from crosscloud.registry import DeploymentRegistryService, DeploymentSpec
from crosscloud.policy import Principal
from crosscloud.store import writer
p = Principal('alice',frozenset(),frozenset({'aob:deployments:write'}))
print(DeploymentRegistryService().register(writer(),p,DeploymentSpec.model_validate_json(sys.argv[1]),'one')['deployment_id'])
"""
    replay = subprocess.check_output([sys.executable, "-c", program, spec.model_dump_json()],
                                     env={**os.environ, "AOB_DEMO_DB_PATH": str(config.DEMO_DB_PATH)}, text=True)
    assert replay.strip() == records[0]["deployment_id"]


def test_scoped_bindings_and_side_effect_free_history(database, monkeypatch):
    conn, old_run = database
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(demo_service, "sync_run", forbidden)
    spec = DeploymentSpec(provider="unknown", runtime_type="agent")
    agent = REG.register(conn, ALICE, spec, "agent")["deployment_id"]
    llm = REG.register(conn, ALICE, DeploymentSpec(provider="local", runtime_type="inference"), "llm")["deployment_id"]
    with pytest.raises(ServiceError, match="OBSERVE_ONLY"):
        CMD.start(conn, ALICE, agent)
    executable = DeploymentSpec(provider="existing", runtime_type="agent", connection_mode="public_https",
                                endpoint_ref="ref:test", execution_policy="execute", capabilities={"start"})
    with pytest.raises(ServiceError, match="NOT_SUPPORTED"):
        CMD.start(conn, ALICE, REG.register(conn, ALICE, executable, "execute")["deployment_id"])
    runs = []
    for i, source in enumerate(("east", "west")):
        binding = BindingSpec(agent_deployment_id=agent, inference_deployment_id=llm, source_id=source,
                              remote_run_id="shared", workload_version="v1", deadline=100)
        receipt = CMD.bind(conn, ALICE, binding, source)
        assert receipt == CMD.bind(conn, ALICE, binding, source)
        with pytest.raises(ServiceError, match="NOT_FOUND"):
            CMD.bind(conn, BOB, binding, source)
        runs.append(receipt["run_id"])
        span = dict(source_id=source, run_id="shared", unit_id="private business text", worker_id=None,
                    trace_id="a"*32, span_id=str(i+1)*16, parent_span_id=None, environment="local",
                    span_name="aob.unit.failed", start_time=1., end_time=2., duration_ms=1000.,
                    status="OK", validation_result="unknown", attributes={}, resource={})
        otlp_store.insert_spans(conn, [{**dict.fromkeys(otlp_store.EXTRA), **span}])

    east, west = runs
    restricted = replace(ALICE, sources=frozenset({"east"}))
    assert QUERY.received_spans(restricted) == 1
    assert QUERY.received_spans(ALICE) == 2
    assert QUERY.observation(ALICE, east, "timeline", 10, 0)["items"][0]["span_id"] == "1"*16
    assert QUERY.observation(ALICE, west, "timeline", 10, 0)["items"][0]["span_id"] == "2"*16
    with pytest.raises(ServiceError, match="NOT_FOUND"):
        QUERY.observation(restricted, west, "summary", 10, 0)
    for principal in (restricted, BOB):
        for lookup in (QUERY.get_run_summary, QUERY.get_run_trace, QUERY.get_failed_tasks,
                       QUERY.get_llm_metrics, QUERY.get_gpu_metrics):
            with pytest.raises(ServiceError, match="NOT_FOUND"):
                lookup(principal, west)
    assert QUERY.list_runs(BOB) == []
    before = conn.execute("PRAGMA data_version").fetchone()[0]
    assert QUERY.get_run_summary(ALICE, old_run)["business_status"] == "submitting"
    ids = [QUERY.list_runs(restricted, 1, n)[0]["run_id"] for n in range(2)]
    assert ids == [east, old_run]
    assert QUERY.get_run_trace(ALICE, east)[0]["span_id"] == "1"*16
    assert QUERY.get_run_trace(ALICE, west)[0]["span_id"] == "2"*16
    assert len(QUERY.get_failed_tasks(ALICE, east)) == 1
    assert "private business text" not in json.dumps(QUERY.get_failed_tasks(ALICE, east))
    assert QUERY.get_llm_metrics(ALICE, east)["usage"] is None
    assert QUERY.get_gpu_metrics(ALICE, east)["active_replicas"] is None
    assert conn.execute("PRAGMA data_version").fetchone()[0] == before
    with closing(store.reader()) as ro, pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("DELETE FROM operations")
    with pytest.raises(ServiceError, match="INVALID_PAGE"):
        QUERY.get_run_trace(ALICE, east, 101)
    assert CMD.request_cancel(conn, ALICE, east)["remote_cancel"] == "NOT_SUPPORTED"
    CMD.record_status(conn, ALICE, east, business="failed")
    REG.unregister(conn, ALICE, agent)
    row = QUERY.get_run_summary(ALICE, east)
    assert (row["agent_version"], row["inference_version"], row["cancel_requested"]) == (1, 1, 1)
    assert (row["business_status"], row["telemetry_status"]) == ("failed", "unknown")
    assert REG.get(conn, ALICE, llm)["status"] == "registered"
    assert conn.execute("SELECT COUNT(*) FROM run_dispatch").fetchone()[0] == 0
