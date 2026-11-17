"""Launch the verified Kaggle GPU notebook via Pull -> event adapter -> Push.

The verified Kaggle notebook is the source of truth for CUDA, Qwen and validation.
No reconstructed notebook or kernel-metadata.json is required in the GitHub repo.

The notebook must remain PRIVATE: the staged notebook contains a per-run event key.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
import logging

import config
import demo_auth

KAGGLE_NOTEBOOK_REF = os.getenv("AOB_KAGGLE_NOTEBOOK_REF", "sillypanda5/notebookb061a06458")
KAGGLE_STATUS_MAP = {
    "NEW_SCRIPT": "queued", "QUEUED": "queued", "RUNNING": "running",
    "COMPLETE": "complete", "ERROR": "failed",
    "CANCEL_REQUESTED": "cancelled", "CANCEL_ACKNOWLEDGED": "cancelled",
}
_STATUS_RE = re.compile(r'has status "(?:KernelWorkerStatus\.)?([A-Za-z_]+)"')
_RUN_RE = re.compile(r"^run-[0-9a-f]{16}$")
_REPO_URL_RE = re.compile(r"^https://[A-Za-z0-9._~/-]+(\.git)?$")
DEMO_MODES = ("light", "high_concurrency")
SCALING_CELL_MARKER = "# AOB scaling demo bootstrap"
log = logging.getLogger("kaggle_launcher")
# Matches the raw-body HMAC contract in demo_auth.verify_event().
PRELUDE_BODY = '''import hashlib, hmac, json, time, urllib.request, uuid

def aob_emit(event_type, payload=None):
    """Send a signed event to the local Agent Ops Board through its public HTTPS URL."""
    body = json.dumps({"event_id": uuid.uuid4().hex, "run_id": AOB_RUN_ID,
                       "event_type": event_type, "ts": time.time(),
                       "payload": payload or {}}).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(_AOB_KEY.encode("utf-8"),
                                     timestamp.encode("ascii") + b"." + body,
                                     hashlib.sha256).hexdigest()
    for attempt in range(3):
        request = urllib.request.Request(
            AOB_EVENTS_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "X-AOB-Timestamp": timestamp,
                     "X-AOB-Signature": signature})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                response.read()
            return True
        except Exception as exc:
            # Do not print request headers / secrets; preserve validated GPU execution.
            print("[AOB EVENT] delivery failed for", event_type,
                  "attempt", attempt + 1, "error type", type(exc).__name__, flush=True)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False
'''



def render_scaling_cell() -> str:
    """Launch autoscaling demo from kaggle_scaling branch."""

    repo_url = os.getenv(
        "AOB_REPO_URL",
        "https://github.com/5tannerpl/agent-ops-board.git"
    )

    repo_branch = "kaggle_scaling"

    if not _REPO_URL_RE.fullmatch(repo_url):
        raise LaunchError("Invalid AOB_REPO_URL")

    return (
        f"{SCALING_CELL_MARKER}\n"
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "_aob_repo = Path('/kaggle/working/aob-scaling-repo')\n"
        "if not (_aob_repo / 'scaling' / 'bootstrap.py').is_file():\n"
        f"    subprocess.run(['git', 'clone', '--depth', '1', '--branch', {repo_branch!r}, {repo_url!r}, str(_aob_repo)], check=True)\n"
        "sys.path.insert(0, str(_aob_repo))\n"
        "from scaling import bootstrap\n"
        "AOB_SCALING_SUMMARY = bootstrap.run(globals())\n"
    )



class LaunchError(Exception):
    """Launcher input, Kaggle CLI or notebook staging failure."""


def kernel_ref() -> str:
    """Identity of the already-tested Kaggle notebook; no local metadata needed."""
    ref = KAGGLE_NOTEBOOK_REF.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", ref):
        raise LaunchError("AOB_KAGGLE_NOTEBOOK_REF must be owner/notebook-slug")
    return ref


def render_prelude(run_id: str, mode: str = "light", units: int | None = None, key_version: int = 1) -> str:
    if not _RUN_RE.fullmatch(run_id):
        raise LaunchError("Invalid run ID")
    if mode not in DEMO_MODES:
        raise LaunchError("Invalid demo mode")
    if units is not None and (isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= 500):
        raise LaunchError("Invalid demo unit count")
    if not config.EVENT_SECRET or not config.EVENTS_PUBLIC_URL:
        raise LaunchError("AOB_EVENT_SECRET and AOB_EVENTS_URL must be configured")
    if not config.EVENTS_PUBLIC_URL.startswith("https://"):
        raise LaunchError("AOB_EVENTS_URL must be a public HTTPS endpoint")
    return ("# Staged by Agent Ops Board. PRIVATE notebook only.\n"
            f"AOB_RUN_ID = {run_id!r}\n"
            f"AOB_DEMO_MODE = {mode!r}\n"
            f"AOB_DEMO_UNITS = {units if mode == 'high_concurrency' else None!r}\n"
            f"AOB_EVENTS_URL = {config.EVENTS_PUBLIC_URL!r}\n"
            f"_AOB_KEY = {demo_auth.run_event_key(run_id, key_version)!r}\n"
            + PRELUDE_BODY)


def _run_kaggle(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([config.KAGGLE_BIN, *args], capture_output=True,
                              text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise LaunchError("Kaggle CLI not found on the server") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchError("Kaggle CLI timed out") from exc


def _cli_checked(args: list[str], timeout: float, operation: str) -> subprocess.CompletedProcess:
    proc = _run_kaggle(args, timeout)
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        # Avoid forwarding CLI output that could include auth material.
        detail = lines[-1][:160] if lines else f"exit code {proc.returncode}"
        raise LaunchError(f"Kaggle {operation} failed: {detail}")
    return proc


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else str(value)


def _code_cell(source: str) -> dict:
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": source.splitlines(keepends=True)}


def _adapt_old_step6(source: str) -> tuple[str, bool]:
    """Replace ONLY the old HTTPS `send_event` function with a signed adapter.

    Leaves the actual check_model and modelcheck execution unchanged. Fail closed
    when original structure does not match; never silently claim event reporting.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, False
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "send_event"]
    if len(defs) != 1 or isinstance(defs[0], ast.AsyncFunctionDef):
        return source, False
    fn = defs[0]
    # A subsequent Pull may return our own previously submitted version.
    # Its bridge must be preserved, rather than wrapped a second time.
    existing_bridge = "aob_emit(event_type, data)" in ast.get_source_segment(source, fn)

    # Limit adaptation to the verified legacy Step 6 pattern.
    if not all(re.search(r'\bsend_event\(\s*["\']' + re.escape(name) + r'["\']', source)
               for name in ("model_validation_started", "worker_validation_completed",
                            "modelcheck_completed", "model_validation_completed")):
        return source, False
    if existing_bridge:
        return source, True
    lines = source.splitlines(keepends=True)
    replacement = ("def send_event(event_type, data):\n"
                   "    global sent, failed\n"
                   "    # Keep the old Step 6 webhook success/failure accounting.\n"
                   "    ok = aob_emit(event_type, data)\n"
                   "    if ok:\n"
                   "        sent += 1\n"
                   "    else:\n"
                   "        failed += 1\n"
                   "    return ok\n")
    lines[fn.lineno - 1:fn.end_lineno] = [replacement]
    result = "".join(lines)
    # Delete obsolete webhook URL assignments so the old receiver is not used or leaked.
    result = re.sub(r'(?m)^WEBHOOK_URL\s*=\s*[^\n]*(?:\n|$)', '', result)
    result = re.sub(
        r'(?m)^if\s+["\']REPLACE_WITH_YOUR_UUID["\']\s+in\s+WEBHOOK_URL\s*:\s*\n'
        r'(?:[ \t]+[^\n]*\n)+', '', result)
    if re.search(r"\bWEBHOOK_URL\b", result):
        # The previous receiver is referenced elsewhere; do not silently produce a broken batch.
        return source, False
    return result, True


def _select_notebook(directory: Path, meta: dict) -> Path:
    filename = meta.get("code_file")
    if filename:
        path = directory / Path(filename).name
        if path.is_file() and path.suffix == ".ipynb":
            return path
    candidates = list(directory.glob("*.ipynb"))
    if len(candidates) == 1:
        meta["code_file"] = candidates[0].name
        return candidates[0]
    raise LaunchError("Pulled notebook .ipynb not found or ambiguous")


def ensure_step6_counters(source: str) -> str:
    """Ensure Step 6 counters exist before send_event is called."""

    if "def send_event(" not in source:
        return source

    # Remove any earlier counter initialization to avoid duplicates.
    source = re.sub(
        r"(?m)^(?:sent|failed)\s*=\s*0\s*\n",
        "",
        source,
    )

    # Insert initialization before the function definition.
    source = source.replace(
        "def send_event(",
        "sent = 0\nfailed = 0\n\n"
        "def send_event(",
        1,
    )

    return source



def stage_notebook(run_id: str, target: Path, mode: str = "light", units: int | None = None,
                   key_version: int = 1) -> None:
    """Pull latest verified Notebook, add signed event adapter and stage for Push."""
    target.mkdir(parents=True, exist_ok=True)
    _cli_checked(["kernels", "pull", kernel_ref(), "-m", "-p", str(target)],
                 float(os.getenv("AOB_KAGGLE_PULL_TIMEOUT", "180")), "pull")
    metadata_file = target / "kernel-metadata.json"
    try:
        meta = json.loads(metadata_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LaunchError("Pulled kernel-metadata.json is missing or invalid") from exc
    if meta.get("id") != kernel_ref():
        raise LaunchError("Pulled Kaggle notebook ID differs from expected source")
    code_file = _select_notebook(target, meta)
    try:
        notebook = json.loads(code_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise LaunchError("Pulled .ipynb is not valid JSON") from exc
    if notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list):
        raise LaunchError("Unexpected Kaggle notebook format")
    # The Push creates a new version under the same Kaggle notebook ID.
    # On subsequent starts, the Pull retrieves that previous staged version.
    # Strip previous per-run secret and start event before inserting new ones.
    if notebook["cells"] and "AOB_RUN_ID = " in _source(notebook["cells"][0]):
        if "# Staged by Agent Ops Board." not in _source(notebook["cells"][0]):
            raise LaunchError("Unrecognized remote run prelude; refusing to overwrite")
        notebook["cells"].pop(0)
        if notebook["cells"] and _source(notebook["cells"][0]).strip() == \
                'aob_emit("notebook_started", {"status": "running"})':
            notebook["cells"].pop(0)
    # A previously staged scaling cell is replaced, never duplicated.
    notebook["cells"] = [c for c in notebook["cells"] if not _source(c).startswith(SCALING_CELL_MARKER)]
    if any("AOB_RUN_ID = " in _source(c) for c in notebook["cells"]):
        raise LaunchError("Notebook still contains another run credential cell")

    adapted = 0
    for cell in notebook["cells"]:
        if cell.get("cell_type") != "code":
            continue     
        updated, changed = _adapt_old_step6(_source(cell))
        if changed:
            adapted += 1

            # Ensure counters exist AFTER legacy Step 6 adaptation.
            updated = ensure_step6_counters(updated)

            # Verify the source that will actually be pushed.
            if not re.search(r"(?m)^sent\s*=\s*0\s*$", updated):
                raise LaunchError("Step 6 missing sent initialization")

            if not re.search(r"(?m)^failed\s*=\s*0\s*$", updated):
                raise LaunchError("Step 6 missing failed initialization")

            cell["source"] = updated.splitlines(keepends=True)
            cell["outputs"] = []
            cell["execution_count"] = None


    if adapted != 1:
        raise LaunchError(
            "Expected exactly one legacy Step 6 send_event function with four validation events; "
            "no changes submitted. Inspect the original Kaggle Notebook.")

    # Report when the notebook actually starts; no claims of readiness before /health.
    notebook["cells"].insert(0, _code_cell(render_prelude(run_id, mode, units, key_version)))
    notebook["cells"].insert(1, _code_cell('aob_emit("notebook_started", {"status": "running"})\n'))
    notebook["cells"].append(_code_cell(render_scaling_cell()))
    for cell in notebook["cells"]:
        if cell.get("cell_type") == "code":
            cell["execution_count"] = None
            cell["outputs"] = []
    code_file.write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")

    # Preserve Kaggle GPU settings known to work, and never pin an old docker image.
    
    # Preserve verified GPU configuration.
    meta["is_private"] = True
    meta["enable_gpu"] = True
    meta["enable_tpu"] = False
    meta["enable_internet"] = True
    meta["machine_shape"] = "NvidiaTeslaT4"

    # IMPORTANT: Remove the pinned Docker image entirely.
    meta.pop("docker_image", None)

    # Avoid a notebook referencing its own previous output.
    meta["kernel_sources"] = []

    # Verify the two required datasets are available.
    required_datasets = {
        "sillypanda5/agent-ops-qwen-model",
        "sillypanda5/agent-ops-llama-cuda-t4",
    }

    dataset_sources = meta.get("dataset_sources", [])
    if not isinstance(dataset_sources, list):
        raise LaunchError("Invalid dataset_sources in Kaggle metadata")

    if not required_datasets.issubset(set(dataset_sources)):
        raise LaunchError(
            "Required Qwen model or CUDA binary dataset is missing"
        )

    # Write UTF-8 without BOM.
    metadata_file.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    # Verify the staged metadata before Kaggle Push.
    staged_meta = json.loads(
        metadata_file.read_text(encoding="utf-8")
    )

    if "docker_image" in staged_meta:
        raise LaunchError(
            "docker_image still exists in staged Kaggle metadata"
        )

    if staged_meta.get("machine_shape") != "NvidiaTeslaT4":
        raise LaunchError("Incorrect Kaggle GPU configuration")

    
    # DO NOT guess or replace dataset_sources (must be exactly those of verified remote Notebook).
    metadata_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")



def submit(run_id: str, mode: str = "light", units: int | None = None, key_version: int = 1) -> None:
    """Submit a Kaggle batch and log final metadata before Push."""

    with tempfile.TemporaryDirectory(prefix="aob-kaggle-") as tmp:
        target = Path(tmp)

        # 1. Pull → Adapt → Remove docker_image → Stage
        stage_notebook(run_id, target, mode, units, key_version)

        # 2. Read the actual metadata prepared for Push
        metadata_file = target / "kernel-metadata.json"

        meta = json.loads(
            metadata_file.read_text(encoding="utf-8-sig")
        )

        # 3. Print final metadata to FastAPI console
        log.warning(
            "\n========== KAGGLE FINAL METADATA (BEFORE PUSH) ==========\n"
            "%s\n"
            "docker_image exists: %s\n"
            "=========================================================",
            json.dumps(meta, ensure_ascii=False, indent=2),
            "docker_image" in meta
        )

        # 4. Push ONCE
        log.warning("Starting Kaggle Push: %s", kernel_ref())

        _cli_checked(
            [
                "kernels",
                "push",
                "-p",
                str(target),
                "--accelerator",
                config.KAGGLE_ACCELERATOR
            ],
            config.KAGGLE_PUSH_TIMEOUT,
            "push"
        )

        # 5. Push accepted
        log.warning(
            "Kaggle Push accepted: %s (run_id=%s)",
            kernel_ref(),
            run_id
        )



def kernel_status() -> str:
    proc = _run_kaggle(["kernels", "status", kernel_ref()], 60)
    match = _STATUS_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not match:
        raise LaunchError("Could not read Kaggle kernel status")
    try:
        return KAGGLE_STATUS_MAP[match.group(1).upper()]
    except KeyError as exc:
        raise LaunchError(f"Unknown Kaggle status {match.group(1)!r}") from exc
