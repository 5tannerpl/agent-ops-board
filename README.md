# Agent Ops Board

Monitoring demo for a multi-channel **Intake Agent**: synthetic email, chat and voice-transcript units are
queued, processed by Python workers that call a locally hosted Qwen model, validated (evidence quotes are
checked against the source text), persisted, and handed to a **mock** downstream orchestrator. A Streamlit UI
(three pages only) shows load-test progress, worker health and per-unit evidence.

All content is synthetic. No classification accuracy/F1, no training, no live voice, no real mailbox,
no external replies or account updates.

## Layout

| Path | Role |
|---|---|
| `adapters/` | Synthetic email/chat/voice generators |
| `intake/` | Canonical intake (`service.py`), prompt + strict output validation (`schema.py`) |
| `storage/` | SQLite (WAL) schema/transactions, raw-file store with traversal protection |
| `workers/` | Queue (atomic claim, leases, fencing, retries), worker (6 steps), model clients, supervisor |
| `orchestration/` | Handoff targets + mock orchestrator (idempotent by `event_id`) |
| `loadtest/` | Scenarios, producer, metrics, `performance.csv` / `summary.json`, headless CLI |
| `api/` | FastAPI endpoints (`/v1/...`, per the design) |
| `dashboard/` | Streamlit: `app.py` (Dashboard), `pages/worker_monitor.py`, `pages/intake_unit_detail.py` |
| `runtime/` | `intake.db`, `raw/`, `attachments/`, `results/{test_run_id}/` (git-ignored) |

## Run

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# 1. Model server (real inference). Ollama:
ollama pull qwen2.5:3b            # or llama.cpp: llama-server -m qwen.gguf --port 8080
export AOB_MODEL_BACKEND=ollama   # ollama | llamacpp | mock
export AOB_MODEL_NAME=qwen2.5:3b

# 2. Workers (one shared model server; N processes). Count follows the load-test request.
python -m workers.supervisor --workers 2

# 3. API and UI
uvicorn api.main:app --port 8000
streamlit run dashboard/app.py          # AOB_API_URL defaults to http://127.0.0.1:8000
```

Click **Start Load Test** in the UI, or run headless: `python -m loadtest --scenario burst_350 --workers 2`
(add `--inject-faults` for the controlled-failure demo). Results land in `runtime/results/<run>/`.

`AOB_MODEL_BACKEND=mock` is a rule-based stand-in for development/tests. The UI labels it **SIMULATED**;
its timings are not throughput evidence. `AOB_MOCK_LATENCY` adds an optional delay purely to slow demos down.

## Behaviour

- **Scenarios:** `multi_channel_350` (arrivals spread over `arrival_window_seconds`, completion deadline
  applies) and `burst_350` (all units at t=0, measures drain time). Only one run may be active (HTTP 409).
  *Stop Test* stops producing; queued jobs are never discarded.
- **Acceptance:** all target units `COMPLETED` and handed off within the deadline. Any `FAILED` /
  `MANUAL_REVIEW` unit fails it and stays visible. Whether 350-in-10-min is achievable depends on hardware and
  model size; the app only records measured results (throughput, P50/P95, queue wait, inference, persistence,
  handoff breakdown, hardware, model/server).
- **Steps:** `NORMALIZE_INPUT → LOAD_CONTEXT → LLM_INFERENCE → VALIDATE_OUTPUT → SAVE_RESULT → HANDOFF`.
  The model output is checkpointed; a resumed attempt skips completed durable work (shown as `SKIPPED`).
  Result + handoff outbox event are saved in one transaction; delivery is idempotent.
- **Evidence:** the model returns verbatim quotes; offsets are computed by the app against stored segment
  text. Unmatched quotes are shown as *Unverified evidence* and never highlighted.
- **Recovery:** heartbeat + lease with a lease-version fencing check on every write; expired leases are
  re-claimed. Retries at ~5 s and ~15 s (+jitter, `AOB_RETRY_DELAYS`), then `FAILED`. Invalid JSON gets one
  repair re-inference, then `MANUAL_REVIEW`; resource exhaustion puts the worker in `BACKOFF`.
- **Fault injection** (per-unit, attempt 1 only): `LLM_TIMEOUT_ONCE`, `INVALID_JSON_ONCE`,
  `HANDOFF_FAIL_ONCE`, `WORKER_CRASH_ONCE` (worker dies silently; supervisor restarts it after 5 s),
  `RESOURCE_EXHAUSTED_ONCE`.

## UI notes

Streamlit cannot route `/workers/{id}`; pages use `/worker_monitor?worker_id=…` and
`/intake_unit_detail?unit_id=…` (shareable). Select a row's checkbox in a table to open its detail page.

## Tests

`python -m pytest` (uses the mock model; covers evidence validation, leasing/fencing, crash recovery,
retries, outbox, load-test fault run and API reads).

## Not production

No authentication/RBAC, encryption or retention; SQLite as queue; retry intervals are demo values.
