"""Central configuration, overridable through AOB_* environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("AOB_RUNTIME_DIR", ROOT / "runtime")).resolve()
RAW_DIR = RUNTIME_DIR / "raw"
ATTACHMENT_DIR = RUNTIME_DIR / "attachments"
RESULTS_DIR = RUNTIME_DIR / "results"
DB_PATH = Path(os.environ.get("AOB_DB_PATH", RUNTIME_DIR / "intake.db"))

MAX_RAW_BYTES = 1_000_000
MAX_TEXT_CHARS = 20_000

SCHEMA_VERSION = "1"

# Model serving. backend: ollama | llamacpp | mock (mock is NOT real inference).
MODEL_BACKEND = os.environ.get("AOB_MODEL_BACKEND", "ollama")
MODEL_NAME = os.environ.get("AOB_MODEL_NAME", "qwen2.5:3b")
MODEL_URL = os.environ.get(
    "AOB_MODEL_URL",
    "http://127.0.0.1:8080" if MODEL_BACKEND == "llamacpp" else "http://127.0.0.1:11434",
)
MODEL_TIMEOUT = float(os.environ.get("AOB_MODEL_TIMEOUT", "120"))
MOCK_LATENCY = float(os.environ.get("AOB_MOCK_LATENCY", "0"))

# Worker / lease behaviour.
LEASE_SECONDS = float(os.environ.get("AOB_LEASE_SECONDS", "20"))
HEARTBEAT_SECONDS = float(os.environ.get("AOB_HEARTBEAT_SECONDS", "2"))
POLL_SECONDS = float(os.environ.get("AOB_POLL_SECONDS", "0.25"))
MAX_ATTEMPTS = int(os.environ.get("AOB_MAX_ATTEMPTS", "3"))
# Delay before attempt 2 and 3; jitter is added on top. Demo values, not production defaults.
RETRY_DELAYS = [float(x) for x in os.environ.get("AOB_RETRY_DELAYS", "5,15").split(",")]
RESOURCE_BACKOFF_SECONDS = float(os.environ.get("AOB_RESOURCE_BACKOFF", "10"))

# Worker health thresholds derived from heartbeat age.
UNHEALTHY_AFTER = float(os.environ.get("AOB_UNHEALTHY_AFTER", "8"))
OFFLINE_AFTER = float(os.environ.get("AOB_OFFLINE_AFTER", "20"))

API_URL = os.environ.get("AOB_API_URL", "http://127.0.0.1:8000")

STEPS = [
    "NORMALIZE_INPUT",
    "LOAD_CONTEXT",
    "LLM_INFERENCE",
    "VALIDATE_OUTPUT",
    "SAVE_RESULT",
    "HANDOFF",
]
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "MANUAL_REVIEW")
