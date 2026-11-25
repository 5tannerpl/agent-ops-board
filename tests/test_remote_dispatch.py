import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import config
import demo_auth
import demo_store
from api.private_app import create_app
from crosscloud import dispatch, resolution, store
from crosscloud.azure import AzureAgentRunClient
from crosscloud.policy import ServiceError
from crosscloud.registry import DeploymentRegistryService, DeploymentSpec
from test_azure_otlp_live import serve
from test_crosscloud import ALICE, SCOPES, forbidden
from test_demo_api import auth, demo
from api.public_ingress import app as ingress


@pytest.fixture
def setup(demo, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CROSS_CLOUD_ENABLED", True)
    monkeypatch.setattr(config, "REMOTE_RUNS_ENABLED", True)
    monkeypatch.setattr(config, "OTLP_READ_SOURCES", '["east"]')
    grants = {"enabled": True, "external_sources": ["east"], "cross_cloud_scopes": SCOPES.split()}
    monkeypatch.setattr(demo_auth, "load_allowed_users", lambda: {"alice": grants, "bob": grants})
    monkeypatch.setenv("AOB_AZURE_RUN_HOST", "sample.example")
    # Numeric pin is never contacted: every client test installs an isolated transport.
    monkeypatch.setenv("AOB_AZURE_RUN_IPV4", "93.184.216.34")
    with TestClient(create_app()) as api:
        def register(provider="azure"):
            spec = DeploymentSpec(provider=provider, runtime_type="agent", connection_mode="public_https",
                                  source_id="east", execution_policy="execute", capabilities={"start", "status"},
                                  endpoint_ref=f"ref:{provider}-agent", credential_ref=f"ref:{provider}-run")
            with closing(store.writer()) as conn:
                d = DeploymentRegistryService().register(conn, ALICE, spec, provider)
            monkeypatch.setenv(f"AOB_{provider.upper()}_RUN_DEPLOYMENT_ID", d["deployment_id"])
            return {"agent_deployment_id": d["deployment_id"], "workload_version":
                    "synthetic-intake-v1" if provider == "azure" else "kaggle-light-v1"}
        yield api, register, grants, dict(auth("alice", scope=SCOPES), **{"Idempotency-Key": "one"})


URL = "/api/v1/runs"


def due():
    with closing(store.writer()) as conn:
        conn.execute("UPDATE dispatch_context SET next_check=0")


def test_admission_replay_revocation_and_history(setup, monkeypatch):
    api, register, grants, headers = setup
    body = register()
    monkeypatch.setattr(resolution, "resolve", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert TestClient(ingress).post(URL, json=body, headers=headers).status_code == 404
    post = lambda **kw: api.post(URL, json=body, **kw)
    assert post().status_code == 401
    assert post(headers=dict(headers, **auth("alice", scope="aob:runs:read"))).status_code == 403
    assert post(headers=dict(headers, **auth("bob", scope=SCOPES))).status_code == 404
    monkeypatch.setattr(config, "REMOTE_RUNS_ENABLED", False)
    assert post(headers=headers).json()["code"] == "EXECUTION_DISABLED"
    monkeypatch.setattr(config, "REMOTE_RUNS_ENABLED", True)
    for bad in ({"workload_version": "other"}, {"endpoint_ref": "x"}, {"deadline_seconds": 121}):
        assert api.post(URL, json={**body, **bad}, headers=headers).status_code == 422
    with ThreadPoolExecutor(8) as pool:
        receipts = list(pool.map(lambda _: post(headers=headers), range(12)))
    assert all(r.status_code == 202 and r.json() == receipts[0].json() for r in receipts)
    path = URL + "/" + receipts[0].json()["run_id"]
    assert api.post(URL, json=dict(body, deadline_seconds=90), headers=headers).status_code == 409
    assert post(headers={**headers, "Idempotency-Key": "other"}).json()["code"] == "RUN_ACTIVE"
    assert api.get(path, headers=headers).json()["dispatch"]["state"] == "reserved"
    with closing(store.writer()) as conn, pytest.raises(demo_store.Conflict):
        demo_store.create_run(conn, "alice")
    grants["external_sources"] = []
    assert post(headers=headers).status_code == 404
    assert api.get(path, headers=headers).status_code == 404
    assert dispatch.step()
    grants["external_sources"] = ["east"]
    assert api.get(path, headers=headers).json()["dispatch"]["last_error_code"] == "NOT_FOUND"


def test_worker_death_then_status_only_recovery(setup, monkeypatch):
    api, register, _, headers = setup
    local = api.post(URL, json=register(), headers=headers).json()["run_id"]
    path = URL + "/" + local
    remote = api.get(path, headers=headers).json()["remote_run_id"]
    # A separate worker exits at the network boundary, after commit but before recording a reply.
    program = """
import os,sys,config,demo_auth
from crosscloud import dispatch,resolution
config.CROSS_CLOUD_ENABLED=config.REMOTE_RUNS_ENABLED=True
config.OTLP_READ_SOURCES='["east"]'
demo_auth.load_allowed_users=lambda: {'alice':{'enabled':True,'external_sources':['east'],
                                              'cross_cloud_scopes':['aob:runs:execute']}}
class Client:
    def start_run(self,ctx):
        assert ctx['remote_run_id']==sys.argv[1]
        os._exit(73)
resolution.resolve=lambda *a: Client()
dispatch.step()
"""
    proc = subprocess.run([sys.executable, "-c", program, remote], timeout=15,
                          env=dict(os.environ, AOB_DEMO_DB_PATH=str(config.DEMO_DB_PATH)))
    assert proc.returncode == 73
    gets = []
    class Client:
        def get_status(self, run_id):
            gets.append(run_id)
            return dispatch.uncertain("REMOTE_UNKNOWN")
    monkeypatch.setattr(resolution, "resolve", lambda *a: Client())
    due()
    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(lambda _: dispatch.step(), range(16))) == 1
    assert gets == [remote]
    assert api.get(path, headers=headers).json()["dispatch"] == {
        "state": "uncertain", "attempts": 1, "last_error_code": "REMOTE_UNKNOWN"}
    with closing(store.writer()) as conn:
        conn.execute("UPDATE deployment_bindings SET deadline=0")
    due()
    assert dispatch.step() and not dispatch.step()
    assert gets == [remote]
    assert api.get(path, headers=headers).json()["dispatch"]["state"] == "uncertain"


def test_reserved_deadline_blocks_first_send(setup, monkeypatch):
    api, register, _, headers = setup
    path = URL + "/" + api.post(URL, json=register(), headers=headers).json()["run_id"]
    monkeypatch.setattr(resolution, "resolve", forbidden)
    with closing(store.writer()) as conn:
        conn.execute("UPDATE deployment_bindings SET deadline=0")
    assert dispatch.step()
    assert api.get(path, headers=headers).json()["dispatch"]["state"] == "terminal"


def test_kaggle_durable_path_shares_callbacks_and_admission(setup, demo):
    api, register, _, headers = setup
    body = register("kaggle")
    path = URL + "/" + api.post(URL, json=body, headers=headers).json()["run_id"]
    run = api.get(path, headers=headers).json()
    assert run["remote_cancel"] == "NOT_SUPPORTED"
    assert dispatch.step() and demo.pushed == [run["remote_run_id"]]
    assert demo.post("/api/demo/start", headers=auth("alice")).status_code == 409
    demo.kaggle["value"] = "running"
    due()
    assert dispatch.step()
    demo.kaggle["value"] = "complete"
    with closing(store.writer()) as conn:
        conn.execute("UPDATE demo_runs SET last_synced_at=0")
    due()
    assert dispatch.step()
    view = api.get(path, headers=headers).json()
    assert view["dispatch"]["state"] == "terminal" and view["business_status"] == "unknown"
    assert api.get(URL + "/" + run["remote_run_id"], headers=headers).status_code == 404


def test_approved_pin_and_credential_rotation(setup, monkeypatch, tmp_path):
    api, register, _, headers = setup
    deployment = api.get("/api/v1/deployments/" + register()["agent_deployment_id"], headers=headers).json()
    token_file = tmp_path / "run-bearer"
    monkeypatch.setenv("AOB_AZURE_RUN_TOKEN_FILE", str(token_file))
    for addr in ("127.0.0.1", "169.254.169.254", "10.0.0.1", "224.0.0.1", "::1", "localhost"):
        monkeypatch.setenv("AOB_AZURE_RUN_IPV4", addr)
        with pytest.raises(ServiceError, match="TARGET_CONFIGURATION"):
            resolution.approved_target(deployment)
    monkeypatch.setenv("AOB_AZURE_RUN_IPV4", "93.184.216.34")
    with pytest.raises(ServiceError, match="TARGET_NOT_APPROVED"):
        resolution.approved_target(dict(deployment, credential_ref="ref:another"))
    target = resolution.approved_target(deployment)
    for _ in range(2):
        token = secrets.token_urlsafe(32)
        token_file.write_text(token)
        client = resolution.resolve(deployment, target, time.time()+30)
        assert client._token == token
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", json.dumps({"east": token}))
    with pytest.raises(ServiceError, match="RUN_CREDENTIAL_UNAVAILABLE"):
        resolution.resolve(deployment, target, time.time()+30)
    monkeypatch.setenv("AOB_AZURE_RUN_HOST", "another.example")
    with pytest.raises(ServiceError, match="TARGET_CHANGED"):
        resolution.resolve(deployment, target, time.time()+30)


@pytest.mark.parametrize("reply,expected", [(307, "uncertain"), (409, "terminal"), (504, "uncertain"),
                                            (401, "terminal"), ("lost", "uncertain")])
def test_remote_failure_classification_and_redaction(setup, monkeypatch, caplog, reply, expected):
    api, register, _, headers = setup
    token = secrets.token_urlsafe(32)
    calls = []
    def send(request):
        calls.append(request)
        if reply == "lost":
            raise httpx.ReadTimeout(token)
        return httpx.Response(reply, headers={"Location": "https://not-approved.invalid"}, content=token)
    monkeypatch.setattr(resolution, "resolve", lambda d,t,end: AzureAgentRunClient(
        **t, token=token, deadline=end, transport=httpx.MockTransport(send)))
    path = URL + "/" + api.post(URL, json=register(), headers=headers).json()["run_id"]
    assert dispatch.step()
    view = api.get(path, headers=headers)
    assert view.json()["dispatch"]["state"] == expected
    assert len(calls) == 1 and calls[0].headers["Authorization"] == "Bearer " + token
    assert token not in caplog.text + view.text
    with closing(store.reader()) as conn:
        assert token not in "\n".join(conn.iterdump())


def test_dispatch_to_actual_sample(setup, monkeypatch, tmp_path):
    python = os.getenv("AOB_AZURE_TEST_PYTHON")
    if not python:
        pytest.skip("set AOB_AZURE_TEST_PYTHON to the locked sibling interpreter")
    api, register, _, headers = setup
    sample = Path(__file__).resolve().parents[2] / "aob-cloud-connectors/samples/azure-python-agent"
    api_token, ingest_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    monkeypatch.setattr(config, "OTLP_SOURCE_TOKENS", json.dumps({"east": ingest_token}))
    trace = "00-" + "aa"*16 + "-" + "bb"*8 + "-01"
    with serve(ingress) as public, socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        proc = subprocess.Popen([python, "-m", "uvicorn", "app.main:create_app", "--factory", "--fd",
                                 str(sock.fileno()), "--no-access-log"], cwd=sample, pass_fds=(sock.fileno(),),
                                env={"RUN_API_TOKEN": api_token, "AOB_EXPORTER": "otlp", "OTLP_SOURCE_TOKEN": ingest_token,
                                     "AOB_OTLP_FINALIZE": "1", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": public+"/v1/traces"})
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1) as health:
                end = time.monotonic()+10
                while True:
                    try:
                        if health.get("/healthz").is_success:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic()<end and proc.poll() is None
                    time.sleep(.02)
            calls = []
            class Loopback(httpx.AsyncHTTPTransport):
                async def handle_async_request(self, request):
                    request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
                    calls.append((request.method, request.url.path))
                    response = await super().handle_async_request(request)
                    if request.method == "POST":
                        assert response.status_code == 200
                        await response.aclose()
                        raise httpx.ReadTimeout("response lost after sample completed")
                    return response
            def client(d, t, end):
                return AzureAgentRunClient(**t, token=api_token, deadline=end, transport=Loopback())
            monkeypatch.setattr(resolution, "resolve", client)
            local = api.post(URL, json=register(), headers={**headers, "traceparent": trace}).json()["run_id"]
            assert dispatch.step()
            assert api.get(URL+"/"+local, headers=headers).json()["dispatch"]["state"] == "uncertain"
            due()
            assert dispatch.step()
            view = api.get(URL+"/"+local, headers=headers).json()
            assert (view["business_status"], view["dispatch"]["state"]) == ("complete", "terminal")
            remote = view["remote_run_id"]
            assert calls == [("POST", "/demo/runs"), ("GET", "/demo/runs/"+remote)]
            make = lambda: client(None, {"address":"93.184.216.34", "hostname":"sample.example"}, time.time()+10)
            assert make().get_status(remote)["business_status"] == "complete"
            assert make().get_units(remote) == {"distinct_units":100, "workers":4}
            with closing(store.reader()) as conn:
                import otlp_store
                s = otlp_store.summary(conn, "east", remote)
                assert (s["trace_status"], s["span_count"], s["external_parent_links"], s["llm_calls"]) == (
                    "TRACE_COMPLETE", 405, 1, 0)
                assert conn.execute("SELECT COUNT(*) FROM external_agent_spans WHERE run_id=? AND trace_id=? "
                                    "AND parent_span_id=?", (remote, "aa"*16, "bb"*8)).fetchone()[0] == 1
                assert not any(t in "\n".join(conn.iterdump()) for t in (api_token, ingest_token))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
