"""Kaggle CLI launcher: submits the GPU batch notebook and reads its status.

The Kaggle notebook never sees Auth0 credentials. Each push stages a copy of the notebook with an injected
first cell holding the run_id, the events URL and a signing key valid only for that run.
"""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import config
import demo_auth

KAGGLE_STATUS_MAP = {
    "NEW_SCRIPT": "queued", "QUEUED": "queued", "RUNNING": "running", "COMPLETE": "complete",
    "ERROR": "failed", "CANCEL_REQUESTED": "cancelled", "CANCEL_ACKNOWLEDGED": "cancelled",
}
_STATUS_RE = re.compile(r'has status "(?:KernelWorkerStatus\.)?([A-Za-z_]+)"')

PRELUDE_BODY = '''
import hashlib, hmac, json, time, urllib.request, uuid


def aob_emit(event_type, payload=None):
    """POST a signed event to the Agent Ops Board. Send only counts, timings and status; no PII or prompts."""
    body = json.dumps({"event_id": uuid.uuid4().hex, "run_id": AOB_RUN_ID, "event_type": event_type,
                       "ts": time.time(), "payload": payload or {}}).encode()
    ts = str(int(time.time()))
    sig = "sha256=" + hmac.new(_AOB_KEY.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(AOB_EVENTS_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-AOB-Timestamp": ts, "X-AOB-Signature": sig})
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception:
            time.sleep(2 ** attempt)
    return False
'''


class LaunchError(Exception):
    pass


def render_prelude(run_id: str) -> str:
    return (f"# Injected by Agent Ops Board for this run; do not edit.\n"
            f"AOB_RUN_ID = {run_id!r}\nAOB_EVENTS_URL = {config.EVENTS_PUBLIC_URL!r}\n"
            f"_AOB_KEY = {demo_auth.run_event_key(run_id)!r}\n{PRELUDE_BODY}")


def _kernel_metadata(directory: Path) -> dict:
    try:
        return json.loads((directory / "kernel-metadata.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LaunchError(f"kernel-metadata.json not readable in {directory}") from exc


def kernel_ref() -> str:
    ref = _kernel_metadata(config.KAGGLE_NOTEBOOK_DIR).get("id")
    if not ref:
        raise LaunchError("kernel-metadata.json has no id")
    return ref


def stage_notebook(run_id: str, target: Path) -> None:
    """Copy the notebook directory to ``target`` and prepend the per-run configuration cell."""
    source = config.KAGGLE_NOTEBOOK_DIR
    meta = _kernel_metadata(source)
    code_file = source / meta.get("code_file", "")
    if not code_file.is_file() or code_file.suffix != ".ipynb":
        raise LaunchError(f"notebook {meta.get('code_file')!r} not found in {source}")
    shutil.copytree(source, target, dirs_exist_ok=True)
    notebook = json.loads(code_file.read_text(encoding="utf-8-sig"))
    notebook["cells"].insert(0, {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
                                 "source": render_prelude(run_id).splitlines(keepends=True)})
    # UTF-8 without BOM, as Kaggle expects.
    (target / code_file.name).write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")


def _run_kaggle(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([config.KAGGLE_BIN, *args], capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise LaunchError("Kaggle CLI not found on the server") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchError("Kaggle CLI timed out") from exc


def submit(run_id: str) -> None:
    """Push the notebook as a GPU batch. Success only means Kaggle accepted the submission."""
    if not config.EVENT_SECRET or not config.EVENTS_PUBLIC_URL:
        raise LaunchError("AOB_EVENT_SECRET and AOB_EVENTS_URL must be configured")
    with tempfile.TemporaryDirectory(prefix="aob-kaggle-") as tmp:
        stage_notebook(run_id, Path(tmp))
        proc = _run_kaggle(["kernels", "push", "-p", tmp, "--accelerator", config.KAGGLE_ACCELERATOR],
                           config.KAGGLE_PUSH_TIMEOUT)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise LaunchError(f"kaggle kernels push failed: {detail[-1][:200] if detail else proc.returncode}")


def kernel_status() -> str:
    """Normalised Kaggle status: queued | running | complete | failed | cancelled."""
    proc = _run_kaggle(["kernels", "status", kernel_ref()], 60)
    match = _STATUS_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not match:
        raise LaunchError("could not read Kaggle kernel status")
    try:
        return KAGGLE_STATUS_MAP[match.group(1).upper()]
    except KeyError:
        raise LaunchError(f"unknown Kaggle status {match.group(1)!r}")
