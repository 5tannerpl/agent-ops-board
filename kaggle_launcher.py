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
import textwrap
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
_CHECKOUT_GUARD = 'if "aob_trace_checkout" in globals():\n    aob_trace_checkout(REPO_DIR, REPO_URL)\nelse:\n'
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
            print(
                "[AOB EVENT] delivery failed for",
                event_type,
                "attempt", attempt + 1,
                "error type", type(exc).__name__,
                "http_status", getattr(exc, "code", None),
                flush=True
            )
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False
'''



def render_scaling_cell(traced: bool = False) -> str:
    """Launch autoscaling demo from kaggle_scaling branch."""

    repo_url = os.getenv(
        "AOB_REPO_URL",
        "https://github.com/5tannerpl/agent-ops-board.git"
    )

    #repo_branch = "kaggle_scaling"
    repo_branch = "OPENTELE"

    if not _REPO_URL_RE.fullmatch(repo_url):
        raise LaunchError("Invalid AOB_REPO_URL")

    checkout = "_aob_repo = REPO_DIR\n" if traced else (
        "from pathlib import Path\n"
        "_aob_repo = Path('/kaggle/working/aob-scaling-repo')\n"
        "if not (_aob_repo / 'scaling' / 'bootstrap.py').is_file():\n"
        f"    subprocess.run(['git', 'clone', '--depth', '1', '--branch', {repo_branch!r}, {repo_url!r}, str(_aob_repo)], check=True)\n"
    )
    repo_commit = _trace_commit() if traced else None
    otel_setup = (
        "import os\n"
        "os.environ['AOB_OTEL_ENABLED'] = 'true'\n"
        "os.environ['AOB_OTEL_EXPORTER'] = 'console'\n"
        "os.environ['AOB_OTEL_SERVICE_NAME'] = 'aob-kaggle-worker'\n"
        "os.environ['AOB_OTEL_ENVIRONMENT'] = 'kaggle-demo'\n"
        f"os.environ['AOB_OTEL_COMMIT'] = {repo_commit!r}\n"
        "os.environ['AOB_RUN_ID'] = AOB_RUN_ID\n"
        "print('OTel enabled:', os.environ.get('AOB_OTEL_ENABLED'))\n"
        "print('Run ID:', os.environ.get('AOB_RUN_ID'))\n"
    ) if traced else ""

    return (
        f"{SCALING_CELL_MARKER}\n"
        "import subprocess, sys\n"
        f"{checkout}"
        "sys.path.insert(0, str(_aob_repo))\n"
        "from scaling import bootstrap\n"
        f"{otel_setup}"
        "AOB_SCALING_SUMMARY = bootstrap.run(globals())\n"
    )

TRACE_CHECK_CELL_MARKER = "# AOB_WORKER_TRACE_VERIFICATION"


def _trace_commit() -> str:
    commit = os.getenv("AOB_OTEL_COMMIT", "")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise LaunchError("Traced notebooks require AOB_OTEL_COMMIT (full Git commit SHA)")
    return commit


def build_trace_check_cell() -> str:
    return textwrap.dedent(
        f"""
        {TRACE_CHECK_CELL_MARKER}
        import json
        import os
        from collections import Counter
        from pathlib import Path

        trace_log = Path("/kaggle/working/aob-runtime/logs/supervisor.log")
        spans = []
        if trace_log.is_file():
            try:
                for trace_line in trace_log.read_text(errors="replace").splitlines():
                    try:
                        span = json.loads(trace_line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(span, dict) and isinstance(span.get("name"), str):
                        spans.append(span)
            except OSError as exc:
                print("TRACE CHECK: could not read supervisor.log:", type(exc).__name__)
        else:
            print("TRACE CHECK: supervisor.log not found")

        span_counts = Counter(span["name"] for span in spans)
        task_spans = [span for span in spans if span["name"] == "aob.worker.task"]
        task_attributes = [
            span.get("attributes") if isinstance(span.get("attributes"), dict) else {{}}
            for span in task_spans
        ]
        unit_ids = {{
            attributes.get("aob.unit_id") for attributes in task_attributes
            if attributes.get("aob.unit_id") is not None
        }}
        missing_unit_ids = sum(
            1 for attributes in task_attributes
            if not isinstance(attributes.get("aob.unit_id"), str) or not attributes.get("aob.unit_id")
        )
        attempt_combinations = [
            (attributes.get("aob.unit_id"), attributes.get("aob.attempt"))
            for attributes in task_attributes
            if attributes.get("aob.unit_id") is not None and attributes.get("aob.attempt") is not None
        ]
        duplicate_attempts = {{
            combination: count for combination, count in Counter(attempt_combinations).items()
            if count > 1
        }}
        run_ids = {{
            attributes.get("aob.run_id") for attributes in
            [span.get("attributes", {{}}) for span in spans]
            if isinstance(attributes, dict) and attributes.get("aob.run_id") is not None
        }}
        missing_run_ids = sum(
            1 for span in spans
            if not isinstance(span.get("attributes"), dict)
            or span["attributes"].get("aob.run_id") is None
        )
        expected_run_id = os.environ.get("AOB_RUN_ID")
        run_id_consistent = bool(spans) and not missing_run_ids and run_ids == {{expected_run_id}}
        diagnostics = []
        if missing_unit_ids:
            diagnostics.append(f"missing unit IDs on {{missing_unit_ids}} task span(s)")
        if duplicate_attempts:
            diagnostics.append(f"duplicate (unit ID, attempt) combinations: {{duplicate_attempts}}")
        if not run_id_consistent:
            diagnostics.append(
                f"run ID mismatch: expected {{expected_run_id}}, found {{sorted(run_ids)}}, "
                f"missing on {{missing_run_ids}} span(s)"
            )

        print("=== AOB WORKER TRACE VERIFICATION ===")
        print("Total Spans:", len(spans))
        print("Worker Task Spans:", len(task_spans))
        print("LLM Inference Spans:", sum(
            count for name, count in span_counts.items() if name.startswith("chat ")
        ))
        print("Validation Spans:", span_counts["aob.result.validation"])
        print("Unique Unit IDs:", len(unit_ids))
        print("Missing Unit IDs:", missing_unit_ids)
        print("Duplicate (Unit ID, Attempt) combinations:", duplicate_attempts)
        print("Run ID consistency:", "PASS" if run_id_consistent else "FAIL")
        if diagnostics:
            print("TRACE CHECK: FAILED -", "; ".join(diagnostics))
        else:
            print("TRACE CHECK: PASSED")
        for span in task_spans[:5]:
            attributes = span.get("attributes", {{}})
            print(
                "Worker:", attributes.get("aob.worker_id"),
                "Unit:", attributes.get("aob.unit_id"),
                "Attempt:", attributes.get("aob.attempt"),
                "Run:", attributes.get("aob.run_id"),
            )
        """
    ).lstrip()



class LaunchError(Exception):
    """Launcher input, Kaggle CLI or notebook staging failure."""


def kernel_ref() -> str:
    """Identity of the already-tested Kaggle notebook; no local metadata needed."""
    ref = KAGGLE_NOTEBOOK_REF.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", ref):
        raise LaunchError("AOB_KAGGLE_NOTEBOOK_REF must be owner/notebook-slug")
    return ref


def _trace_prelude() -> str:
    if os.getenv("AOB_OTEL_ENABLED", "").lower() != "true":
        return ""
    commit = _trace_commit()
    labels = {"AOB_OTEL_SERVICE_NAME": os.getenv("AOB_OTEL_SERVICE_NAME", "aob-kaggle-agent"),
              "AOB_OTEL_ENVIRONMENT": os.getenv("AOB_OTEL_ENVIRONMENT", "kaggle-demo")}
    if not all(re.fullmatch(r"[\w.-]{1,64}", value, re.ASCII) for value in labels.values()):
        raise LaunchError("Invalid AOB tracing resource label")
    if os.getenv("AOB_OTEL_EXPORTER", "console") != "console":
        raise LaunchError("Only console tracing is supported")
    return f'''
import os
os.environ.update({labels!r}, AOB_OTEL_ENABLED="true", AOB_OTEL_EXPORTER="console",
                  AOB_RUN_ID=AOB_RUN_ID, AOB_OTEL_COMMIT={commit!r})

def aob_trace_checkout(directory, url):
    import subprocess, sys
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    git = ["git", "-C", str(directory)]
    subprocess.run(git + ["fetch", "--depth=1", url, {commit!r}], check=True)
    subprocess.run(git + ["checkout", "--detach", {commit!r}], check=True)
    if subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r",
                       str(directory / "kaggle-notebook" / "requirements-otel.txt")]).returncode:
        print("AOB tracing disabled: optional dependency installation failed", flush=True)
        os.environ["AOB_OTEL_ENABLED"] = "false"
'''


def render_prelude(run_id: str, mode: str = "light", units: int | None = None, key_version: int = 1) -> str:
    if not _RUN_RE.fullmatch(run_id):
        raise LaunchError("Invalid run ID")
    if mode not in DEMO_MODES:
        raise LaunchError("Invalid demo mode")
    if units is not None and (isinstance(units, bool) or not isinstance(units, int) or not 1 <= units <= 500):
        raise LaunchError("Invalid demo unit count")
    if not config.get_event_secret() or not config.EVENTS_PUBLIC_URL:
        raise LaunchError("Event signing and AOB_EVENTS_URL must be configured")
    if not config.EVENTS_PUBLIC_URL.startswith("https://"):
        raise LaunchError("AOB_EVENTS_URL must be a public HTTPS endpoint")
    return ("# Staged by Agent Ops Board. PRIVATE notebook only.\n"
            f"AOB_RUN_ID = {run_id!r}\n"
            f"AOB_DEMO_MODE = {mode!r}\n"
            f"AOB_DEMO_UNITS = {units if mode == 'high_concurrency' else None!r}\n"
            f"AOB_EVENTS_URL = {config.EVENTS_PUBLIC_URL!r}\n"
            f"_AOB_KEY = {demo_auth.run_event_key(run_id, key_version)!r}\n"
            + PRELUDE_BODY + _trace_prelude())


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
        # Kaggle CLI output can contain credential or request metadata.
        raise LaunchError(f"Kaggle {operation} failed (exit code {proc.returncode})")
    return proc


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else str(value)


def _code_cell(source: str) -> dict:
    return {"cell_type": "code", "metadata": {}, "execution_count": None,
            "outputs": [], "source": source.splitlines(keepends=True)}


_RAW_OUTPUT_FIELD_RE = re.compile(
    r""",?[ \t]*\n?[ \t]*["'](?:stdout|stderr)["']\s*:\s*result\.(?:stdout|stderr)(?:\[[^\]\n]*\])?""")
_LOCAL_RUN_ID_RE = re.compile(r"(?m)^RUN_ID\s*=\s*str\(\s*uuid\.uuid4\(\)\s*\)[ \t]*$")


def _harden_step6(source: str) -> tuple[str, bool]:
    """Keep raw stdout/stderr out of events and tie Step 6 to the AOB-injected run ID.

    Fails closed (False) when the result is not valid Python or still sends raw output / a local UUID.
    """
    result = _RAW_OUTPUT_FIELD_RE.sub("", source)
    result = _LOCAL_RUN_ID_RE.sub("RUN_ID = AOB_RUN_ID", result)
    try:
        tree = ast.parse(result)
    except SyntaxError:
        return source, False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value in ("stdout", "stderr") for k in node.keys):
            return source, False
    if re.search(r"\buuid\.uuid4\(", result):
        return source, False
    return result, True


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
        return _harden_step6(source)
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
    hardened, ok = _harden_step6(result)
    return (hardened, True) if ok else (source, False)


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
    notebook["cells"] = [
        c for c in notebook["cells"]
        if not _source(c).startswith((SCALING_CELL_MARKER, TRACE_CHECK_CELL_MARKER))
    ]
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

    traced = os.getenv("AOB_OTEL_ENABLED", "").lower() == "true"
    checkouts = 0
    for cell in notebook["cells"]:
        source = _source(cell)
        if source.startswith(_CHECKOUT_GUARD):
            source = textwrap.dedent(source[len(_CHECKOUT_GUARD):])
            cell["source"] = source.splitlines(keepends=True)
        if (traced and cell["cell_type"] == "code" and source.startswith('if (REPO_DIR / ".git").is_dir():')
                and '"pull", "--ff-only"' in source and '"git", "clone"' in source):
            cell["source"] = (_CHECKOUT_GUARD + textwrap.indent(source, "    ")).splitlines(keepends=True)
            checkouts += 1
    if traced and checkouts != 1:
        raise LaunchError("Cannot identify the verified notebook Git checkout for a pinned tracing run")

    # Report when the notebook actually starts; no claims of readiness before /health.
    notebook["cells"].insert(0, _code_cell(render_prelude(run_id, mode, units, key_version)))
    notebook["cells"].insert(1, _code_cell('aob_emit("notebook_started", {"status": "running"})\n'))
    notebook["cells"].append(_code_cell(render_scaling_cell(traced)))
    notebook["cells"].append(_code_cell(build_trace_check_cell()))
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
    """Submit a Kaggle batch without logging notebook metadata."""

    with tempfile.TemporaryDirectory(prefix="aob-kaggle-") as tmp:
        target = Path(tmp)

        # 1. Pull → Adapt → Remove docker_image → Stage
        stage_notebook(run_id, target, mode, units, key_version)

        # Push once; do not expose staged notebook metadata in application logs.
        log.info("Starting Kaggle push for verified notebook")

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

        log.info("Kaggle push accepted")



def kernel_status() -> str:
    proc = _run_kaggle(["kernels", "status", kernel_ref()], 60)
    match = _STATUS_RE.search(proc.stdout or "")
    if proc.returncode != 0 or not match:
        raise LaunchError("Could not read Kaggle kernel status")
    try:
        return KAGGLE_STATUS_MAP[match.group(1).upper()]
    except KeyError as exc:
        raise LaunchError(f"Unknown Kaggle status {match.group(1)!r}") from exc
