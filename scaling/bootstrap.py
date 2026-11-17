"""Notebook entry point: ``bootstrap.run(globals())`` from the cell the launcher appends to the Kaggle notebook.

It reuses whatever the earlier cells already resolved (``LLAMA_SERVER``, ``MODEL_PATH``, the running GPU 0
``server``) and only falls back to discovering the Datasets itself.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scaling.scenario import Environment, run_scenario
from scaling.settings import for_mode
from security.event_text import safe_text

RUNTIME_DIR = Path("/kaggle/working/aob-runtime")
CUDA_DIR = Path("/kaggle/working/llama-cuda")


def _ensure_dependencies() -> None:
    missing = [m for m in ("httpx", "pydantic") if importlib.util.find_spec(m) is None]
    if missing:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *missing], check=True)


def _find_llama_server(ns: dict) -> Path:
    known = ns.get("LLAMA_SERVER")
    if known and Path(known).is_file():
        return Path(known)
    restored = [p for p in CUDA_DIR.rglob("llama-server") if p.is_file()] if CUDA_DIR.exists() else []
    if restored:
        return restored[0]
    found = [p for p in Path("/kaggle/input").rglob("llama-server")
             if p.is_file() and "agent-ops-llama-cuda-t4" in str(p)]
    if found:
        dataset_root = next(p for p in found[0].parents if p.name == "agent-ops-llama-cuda-t4")
        shutil.copytree(dataset_root, CUDA_DIR, symlinks=True, dirs_exist_ok=True)  # keeps the .so symlinks
        server = next(p for p in CUDA_DIR.rglob("llama-server") if p.is_file())
        server.chmod(0o755)
        return server
    raise FileNotFoundError("llama-server not found: attach the agent-ops-llama-cuda-t4 Dataset")


def _find_model(ns: dict) -> Path:
    known = ns.get("MODEL_PATH")
    if known and Path(known).is_file():
        return Path(known)
    ggufs = sorted(p for p in Path("/kaggle/input").rglob("*.gguf") if "agent-ops-qwen-model" in str(p))
    if not ggufs:
        raise FileNotFoundError("GGUF not found: attach the agent-ops-qwen-model Dataset")
    return ggufs[0]


def _library_path(server: Path) -> str:
    root = next((p for p in server.parents if p.name in ("llama-cuda", "agent-ops-llama-cuda-t4")), server.parent)
    dirs = sorted({str(p.parent) for p in root.rglob("*.so*")} | {str(server.parent)})
    return ":".join(dirs + [os.environ.get("LD_LIBRARY_PATH", "")])


def run(ns: dict) -> dict:
    """Run the demo for ``AOB_DEMO_MODE`` and return the summary; raises if the experiment failed."""
    repo_dir = Path(__file__).resolve().parents[1]
    emit = ns["aob_emit"]
    settings = for_mode(ns.get("AOB_DEMO_MODE") or "light", ns.get("AOB_DEMO_UNITS"))
    try:
        _ensure_dependencies()
        server, model = _find_llama_server(ns), _find_model(ns)
    except Exception as exc:  # report it: a silent skip would look like a pass
        emit("scaling_demo_failed", {"status": "failed", "phase": "environment", "mode": settings.mode,
                                     "error_code": type(exc).__name__, "message": safe_text(exc, 300)})
        raise
    llama_env = {**os.environ, "LD_LIBRARY_PATH": _library_path(server)}
    os.environ["LD_LIBRARY_PATH"] = llama_env["LD_LIBRARY_PATH"]
    existing = ns.get("server")
    env = Environment(
        repo_dir=repo_dir, runtime_dir=RUNTIME_DIR, llama_server=server, model_path=model,
        existing_server=existing if isinstance(existing, subprocess.Popen) else None, llama_env=llama_env)
    summary = run_scenario(settings, env, emit)
    print("SCALING DEMO RESULT:", summary.get("status"), summary.get("reasons") or summary.get("error_code"),
          flush=True)
    if summary.get("status") == "failed":
        raise RuntimeError(f"scaling demo failed: {summary.get('error_code')}")
    return summary
