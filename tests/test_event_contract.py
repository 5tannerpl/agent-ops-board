"""Phase 1 event contract: what the Notebook and the Autoscaler really send must pass the ingest schemas, and
anything outside the contract (unknown fields, raw logs, URLs, secrets) must be rejected."""
import io
import json
import logging
import pathlib
import subprocess
import sys
import types
import urllib.error
import urllib.request

import pytest
from pydantic import ValidationError

import config
import demo_store as store
import kaggle_launcher
from security.event_schemas import EVENT_PAYLOAD_MODELS, UnknownEventType, rejection_diagnostics, validate_payload
from tests.test_demo_api import auth, demo, signed_post  # noqa: F401  (fixtures and helpers)

NOTEBOOKS = [config.ROOT / "kaggle-notebook" / "notebookb061a06458.ipynb",
             config.ROOT / "notebook-check" / "notebookb061a06458.ipynb"]
VALIDATION = ["model_validation_started", "worker_validation_completed", "modelcheck_completed",
              "model_validation_completed"]
LEAKY_STDOUT = "ok\n" * 400 + "POST https://leak.example.com/hook?token=abc123\n"


def assert_contract(events) -> None:
    """Every (event_type, payload) an Autoscaler/Scenario producer emitted validates against its schema."""
    assert events
    for event_type, payload in events:
        try:
            validate_payload(event_type, json.loads(json.dumps(payload)))
        except ValidationError as exc:
            pytest.fail(f"{event_type} violates its schema: {rejection_diagnostics(event_type, exc)}")


# ---- Notebook producer, sent through the real signed prelude into the real ingress --------------------------------

def _step6_source(path) -> str:
    cells = ["".join(c["source"]) for c in json.loads(path.read_text("utf-8"))["cells"] if c["cell_type"] == "code"]
    return next(s for s in cells if "Step 6" in s and "model_validation_started" in s)


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"{}"


@pytest.fixture
def notebook_run(demo, monkeypatch):
    """A run whose notebook namespace holds the real injected prelude; urlopen delivers to the ingress app."""
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    sent = []

    def urlopen(request, timeout=None):
        sent.append(json.loads(request.data))
        headers = {k.lower(): v for k, v in request.header_items()}
        resp = demo.ingress.post("/api/events", content=request.data, headers={
            "Content-Type": headers["content-type"], "X-AOB-Timestamp": headers["x-aob-timestamp"],
            "X-AOB-Signature": headers["x-aob-signature"]})
        if resp.status_code != 200:
            raise urllib.error.HTTPError(request.full_url, resp.status_code, "rejected", {}, io.BytesIO(b""))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=LEAKY_STDOUT, stderr="warning: something\n"))
    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=lambda *a, **k: types.SimpleNamespace(
        status_code=200, raise_for_status=lambda: None)))
    monkeypatch.setattr(pathlib.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)
    ns = {"__name__": "notebook"}
    exec(kaggle_launcher.render_prelude(run_id), ns)
    ns.update({"REPO_DIR": pathlib.Path("/kaggle/working/agent-repo"), "API": "http://127.0.0.1:8080",
               "MODEL_NAME": "Qwen3-4B-Instruct-2507", "os": __import__("os"), "sys": sys, "time": __import__("time"),
               "subprocess": subprocess, "Path": pathlib.Path,
               "server": types.SimpleNamespace(terminate=lambda: None)})
    return run_id, ns, sent


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.parent.name)
def test_notebook_step6_events_are_accepted_with_the_aob_run_id(demo, notebook_run, path):
    run_id, ns, sent = notebook_run
    exec(_step6_source(path), ns)

    assert [e["event_type"] for e in sent] == VALIDATION
    assert {e["run_id"] for e in sent} == {run_id}  # the AOB-injected id, never a local uuid
    assert ns.get("RUN_ID", run_id) == run_id
    raw = json.dumps(sent)
    assert '"stdout"' not in raw and '"stderr"' not in raw and "leak.example.com" not in raw
    stored = {e["event_type"]: e["payload"] for e in store.list_events(store.connect(), run_id, 0, 100)}
    assert stored["worker_validation_completed"] == {
        "status": "passed", "test_runs": 10, "duration_seconds": stored["worker_validation_completed"][
            "duration_seconds"], "exit_code": 0}
    assert set(stored["modelcheck_completed"]) == {"status", "duration_seconds", "exit_code"}
    assert stored["model_validation_completed"]["backend"] == "llamacpp"
    assert stored["model_validation_completed"]["api_health"] == 200
    assert demo.get(f"/api/demo/{run_id}/status", headers=auth()).json()["result"] == "passed"


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda p: p.parent.name)
def test_notebook_sources_never_create_a_local_run_id_or_send_raw_output(path):
    source = "\n".join("".join(c["source"]) for c in json.loads(path.read_text("utf-8"))["cells"])
    step6 = _step6_source(path)
    assert "uuid4()" not in step6.replace("uuid.uuid4().hex", "")
    assert '"stdout"' not in step6 and '"stderr"' not in step6
    assert "_AOB_KEY = '" not in source or "redacted" in source  # no committed per-run key


LEGACY_STEP6 = '''import subprocess, sys, time, uuid
RUN_ID = str(uuid.uuid4())
sent = 0
failed = 0

def send_event(event_type, data):
    pass

send_event("model_validation_started", {"test_runs": 10, "status": "running"})
result = subprocess.run([sys.executable, "-m", "workers.check_model"], capture_output=True, text=True)
send_event(
    "worker_validation_completed",
    {
        "test_runs": 10,
        "duration_seconds": 1.5,
        "exit_code": result.returncode,
        "status": (
            "passed"
            if result.returncode == 0
            else "failed"
        ),
        "stdout": result.stdout[-6000:],
        "stderr": result.stderr[-2000:]
    }
)
send_event("modelcheck_completed", {"duration_seconds": 2.0, "exit_code": 0, "status": "passed",
    "stdout": result.stdout[-1500:], "stderr": result.stderr[-500:]})
send_event("model_validation_completed", {"status": "passed", "model": "Qwen3-4B-Instruct-2507",
                                          "backend": "llamacpp", "api_health": 200})
'''


def test_launcher_strips_raw_output_and_local_run_id_from_a_legacy_step6(demo, tmp_path, monkeypatch, notebook_run):
    run_id, ns, sent = notebook_run
    cells = [{"cell_type": "code", "metadata": {}, "outputs": [], "source": LEGACY_STEP6.splitlines(True)}]

    def pull(args, timeout, operation):
        target = pathlib.Path(args[args.index("-p") + 1])
        (target / "kernel-metadata.json").write_text(json.dumps({
            "id": "sillypanda5/notebookb061a06458", "code_file": "nb.ipynb",
            "dataset_sources": ["sillypanda5/agent-ops-qwen-model", "sillypanda5/agent-ops-llama-cuda-t4"]}))
        (target / "nb.ipynb").write_text(json.dumps({"nbformat": 4, "nbformat_minor": 5, "metadata": {},
                                                     "cells": cells}))

    monkeypatch.setattr(kaggle_launcher, "_cli_checked", pull)
    out = tmp_path / "staged"
    kaggle_launcher.stage_notebook(run_id, out)
    staged = ["".join(c["source"]) for c in json.loads((out / "nb.ipynb").read_text())["cells"]]
    step6 = staged[2]
    assert '"stdout"' not in step6 and '"stderr"' not in step6 and "RUN_ID = AOB_RUN_ID" in step6
    exec(step6, ns)
    assert ns["failed"] == 0 and ns["sent"] == 4 and ns["RUN_ID"] == run_id
    assert {e["run_id"] for e in sent} == {run_id}


def test_launcher_fails_closed_when_raw_output_cannot_be_removed():
    source = LEGACY_STEP6.replace('"stdout": result.stdout[-6000:]', '"stdout": some_helper(result)')
    assert kaggle_launcher._adapt_old_step6(source) == (source, False)


# ---- Schema contract -----------------------------------------------------------------------------------------------

VALID = [
    ("notebook_started", {"status": "running"}),
    ("notebook_started", {"gpu_count": 2, "gpus": ["GPU 0: Tesla T4", "GPU 1: Tesla T4"]}),
    ("model_ready", {"api_health": 200, "backend": "llamacpp"}),
    ("model_validation_started", {"test_runs": 10, "model": "Qwen3-4B-Instruct-2507", "status": "running"}),
    ("worker_validation_completed", {"test_runs": 10, "duration_seconds": 41.237, "exit_code": 0,
                                     "status": "passed"}),
    ("worker_validation_completed", {"test_runs": 10, "duration_seconds": 3.0, "exit_code": -9, "status": "failed"}),
    ("modelcheck_completed", {"duration_seconds": 12.5, "exit_code": 0, "status": "passed"}),
    ("model_validation_completed", {"status": "passed", "model": "Qwen3-4B-Instruct-2507", "backend": "llamacpp",
                                   "api_health": 200}),
]


@pytest.mark.parametrize("event_type,payload", VALID)
def test_structured_events_pass_and_keep_their_metadata(event_type, payload):
    assert validate_payload(event_type, payload) == payload


@pytest.mark.parametrize("event_type,payload", [
    ("model_validation_started", {"status": "running", "stdout": "x"}),
    ("worker_validation_completed", {"status": "passed", "exit_code": 0, "stdout": "log", "stderr": ""}),
    ("modelcheck_completed", {"status": "passed", "stderr": "Traceback"}),
    ("model_validation_completed", {"status": "passed", "api_url": "https://x.example"}),
    ("modelcheck_completed", {"duration_seconds": 1.0, "exit_code": 0}),  # status is required
    ("modelcheck_completed", {"status": "ok"}),
    ("worker_validation_completed", {"status": "passed", "exit_code": True}),  # strict: bool is not an int
    ("worker_validation_completed", {"status": "passed", "duration_seconds": float("nan")}),
    ("worker_validation_completed", {"status": "passed", "test_runs": "10"}),
    ("model_validation_completed", {"status": "passed", "backend": "http://169.254.169.254/"}),
    ("model_validation_completed", {"status": "passed", "model": "line1\nline2"}),
    ("notebook_started", {"gpus": ["see https://hook.example/abc"]}),
    ("notebook_started", {"gpus": ["Traceback (most recent call last):\n  File x"]}),
    ("scaling_controller_error", {"error": "KeyError", "message": "failed key=" + "a1b2c3d4" * 8}),
    ("scaling_controller_error", {"error": "KeyError", "message": "Authorization: Bearer abc.def.ghi"}),
    ("gpu_instance_failed", {"gpu_id": 1, "message": "sha256=" + "0" * 64}),
    ("gpu_instance_failed", {"gpu_id": 1, "message": "x" * 301}),
    ("scaling_metrics_snapshot", {"pending": 1, "log_tail": "x"}),
    ("scaling_metrics_snapshot", {"gpu0": {"state": "READY", "host": "10.0.0.1"}}),
    ("scaling_demo_completed", {"status": "passed", "url": "https://x.example"}),
    ("worker_scaling_requested", {"from": 2, "to": 4, "secret": "x"}),
])
def test_unknown_fields_raw_logs_urls_and_secrets_are_rejected(event_type, payload):
    with pytest.raises(ValidationError):
        validate_payload(event_type, payload)


def test_unknown_event_type_is_rejected():
    with pytest.raises(UnknownEventType):
        validate_payload("something_else", {})


def test_every_allowlisted_event_has_a_strict_schema():
    for model in EVENT_PAYLOAD_MODELS.values():
        assert model.model_config["extra"] == "forbid" and model.model_config["strict"] is True


# ---- Autoscaler / scenario producers ------------------------------------------------------------------------------

def test_real_producer_shapes_pass_the_schemas():
    """Literal shapes emitted by scaling/autoscaler.py and scaling/scenario.py (see assert_contract callers in
    tests/test_scaling.py for the live ones)."""
    assert_contract([
        ("scaling_demo_started", {
            "mode": "high_concurrency", "min_workers": 2, "max_workers": 8, "initial_gpu_instances": 1,
            "target_gpu_instances": 2, "planned_units": 350, "arrival_window_seconds": 0, "gpu_count": 2,
            "gpu1_available": True, "model": "Qwen3-4B-Instruct-2507"}),
        ("worker_scaling_requested", {"from": 2, "to": 4, "reason": "queue depth 98 >= 20", "queue_depth": 98,
                                      "cooldown_until": 1760000000.123}),
        ("worker_scaling_completed", {"status": "timeout", "from": 4, "to": 8, "desired_workers": 8,
                                      "actual_workers": 5, "duration_seconds": 61.0}),
        ("gpu_instance_starting", {"gpu_id": 1, "port": 8081, "reason": "backlog 12s; drain 80s"}),
        ("gpu_instance_ready", {
            "gpu_id": 1, "port": 8081, "model": "Qwen3-4B-Instruct-2507", "status": "ready",
            "load_duration_seconds": 28.4, "memory_used_mb": 4300, "memory_delta_mb": 4000,
            "device_binding": "CUDA_VISIBLE_DEVICES=1, --device CUDA0", "device_binding_verified": True}),
        ("gpu_instance_ready", {"gpu_id": 0, "port": 8080, "model": "Qwen3-4B-Instruct-2507", "status": "ready",
                                "load_duration_seconds": None, "adopted": True, "memory_used_mb": None,
                                "device_binding": "adopted"}),
        ("gpu_instance_failed", {"gpu_id": 1, "port": 8081, "error_code": "GPU1_START_FAILED", "message": "",
                                 "attempt": 1, "status": "failed"}),
        ("gpu_instance_draining", {"gpu_id": 1, "inflight_requests": 3}),
        ("gpu_instance_stopped", {"gpu_id": 1, "port": 8081, "exit_code": None, "reason": "queue idle",
                                  "inflight_requests": 0, "port_released": True, "memory_released": None,
                                  "clean": True, "status": "stopped"}),
        ("inference_route_changed", {"active_backends": ["gpu0", "gpu1"], "reason": "GPU 1 ready"}),
        ("scaling_controller_error", {"error": "OperationalError", "message": "database is locked"}),
        ("scaling_metrics_snapshot", {
            "mode": "high_concurrency", "scaling_state": "SCALE_OUT", "pending": 80, "running": 4, "completed": 16,
            "failed": 0, "total": 100, "actual_workers": 4, "desired_workers": 8, "gpu_instances_ready": 1,
            "gpu0": {"state": "READY", "port": 8080, "memory_used_mb": 4800, "utilization_pct": 80.5},
            "gpu1": {"state": "STOPPED", "port": 8081, "memory_used_mb": None, "utilization_pct": None},
            "requests_by_gpu": {"gpu0": 16, "gpu1": 0}, "inflight_by_gpu": {"gpu0": 4, "gpu1": 0},
            "failed_by_gpu": {"gpu0": 0, "gpu1": 0}, "units_by_gpu": {"gpu0": 16, "gpu1": 0},
            "unattributed_units": 0, "oldest_pending_seconds": None, "estimated_drain_seconds": None}),
        ("scaling_demo_completed", {
            "status": "passed", "reasons": [], "mode": "high_concurrency", "total": 350, "completed": 350,
            "failed": 0, "retries": 3, "duration_seconds": 412.5, "p50_seconds": 3.1, "p95_seconds": 9.8,
            "throughput_per_min": 50.9, "peak_workers": 8, "peak_gpu_instances": 2,
            "confirmed_worker_levels": [2, 4, 8], "gpu0_inference_requests": 200, "gpu1_inference_requests": 150,
            "gpu1_stopped_cleanly": True, "final_workers": 2, "final_gpu_instances": 1, "gpu0_units_completed": 200,
            "gpu1_units_completed": 150, "unattributed_units": 0, "scale_out_seconds": 80.0,
            "worker_scale_out_seconds": 80.0, "gpu1_load_seconds": 28.4, "scale_in_seconds": 45.0,
            "telemetry_failed_events": 0}),
        ("scaling_demo_completed", {"status": "degraded", "reasons": ["GPU1_START_FAILED", "WORKERS_4_NOT_CONFIRMED"],
                                    "p50_seconds": None, "gpu1_stopped_cleanly": None, "scale_out_seconds": None}),
        ("scaling_demo_failed", {"status": "failed", "phase": "gpu0", "error_code": "GPU0_START_FAILED",
                                 "message": "RuntimeError: boom", "mode": "light"}),
    ])


def test_producer_text_is_sanitised_to_satisfy_the_schema():
    from security.event_text import safe_text
    nasty = ("RuntimeError: failed\nTraceback (most recent call last):\n  File x\n"
             "GET https://abc.trycloudflare.com/api/events token=" + "f" * 64)
    cleaned = safe_text(nasty, 300)
    assert "\n" not in cleaned and "https://" not in cleaned and "f" * 32 not in cleaned
    assert validate_payload("gpu_instance_failed", {"gpu_id": 1, "message": cleaned})["message"] == cleaned


# ---- Server diagnostics ---------------------------------------------------------------------------------------------

def test_rejected_event_logs_only_event_type_field_path_and_error_type(demo, caplog):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    secret = "s3cr3t-value-" + "z" * 50
    with caplog.at_level(logging.WARNING, logger="demo_api"):
        resp = signed_post(demo, run_id, event_type="worker_validation_completed",
                           payload={"status": "passed", "stdout": secret, "exit_code": "0",
                                    "x" * 60 + secret: 1})
        bad_type = signed_post(demo, run_id, event_id="evt-00000002", event_type="not_an_event", payload={})
    assert resp.status_code == 422 and resp.json() == {"detail": "invalid event"}
    assert bad_type.status_code == 422
    text = caplog.text
    assert "event_type=worker_validation_completed field=payload.stdout error_type=extra_forbidden" in text
    assert "field=payload.exit_code error_type=int_type" in text
    assert "field=payload.<key> error_type=extra_forbidden" in text
    assert "event_type=<unknown> field=event_type error_type=unknown_event_type" in text
    assert secret not in text and "z" * 20 not in text and run_id not in text


def test_hmac_is_still_checked_before_schema_validation(demo):
    run_id = demo.post("/api/demo/start", headers=auth()).json()["run_id"]
    bad_payload = {"status": "passed", "stdout": "log"}
    assert signed_post(demo, run_id, event_type="modelcheck_completed", payload=bad_payload,
                       key_run_id="run-0000000000000000").status_code == 401
    assert signed_post(demo, run_id, event_type="modelcheck_completed", payload=bad_payload).status_code == 422
