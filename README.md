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
| `dashboard/` | Streamlit: `app.py` (Dashboard), `pages/worker_monitor.py`, `pages/intake_unit_detail.py`; `auth_gate.py` (Auth0 login), `live_demo.py` (Live GPU demo panel) |
| `demo_*.py` | Private live demo: `demo_auth.py` (Auth0 JWT, whitelist, event signing), `demo_api.py` (`/api/*`), `demo_service.py` (run lifecycle), `kaggle_launcher.py` (Kaggle CLI), `demo_store.py` (SQLite `runtime/demo.db`) |
| `scaling/` | Autoscaling live demo (runs inside the Kaggle batch): `autoscaler.py`, `policy.py`, `gpu_manager.py`, `inference_router.py` (`:8090`), `scenario.py`, `events.py`, `bootstrap.py` |
| `demo_scaling.py` | Read models behind `/state`, `/metrics`, `/summary` |
| `kaggle-notebook/` | `kernel-metadata.json` for the Kaggle GPU batch (put the `.ipynb` here, see below) |
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

## Checking the real model first

`python -m workers.check_model --runs 3` runs real inference on synthetic units and prints per-unit latency
and output validity. Do this before a load test: 350 units in 600 s needs ~35 units/min in total. On a
CPU-only laptop use a small model (e.g. `qwen2.5:1.5b` / `0.5b`) and 1 worker, because Ollama serialises
requests and queue wait counts against `AOB_MODEL_TIMEOUT` (default 120 s). Use a small unit count (e.g. 20,
`burst_350`) to calibrate.

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

## Private live demo (Auth0 + Kaggle GPU batch)

Only Auth0 users that an admin created **and** listed in `allowed_users.json` can start the demo. There is no
public sign-up. Login success, Kaggle submission and event delivery are three independent states:
a valid login is not authorisation (the whitelist is checked by FastAPI on every request), a successful
`kaggle kernels push` only means `queued` (never "GPU ready"), and an event is only recorded when it arrives.

```
Streamlit (st.login, Auth0 Universal Login) --access token--> FastAPI /api/*  (JWT + whitelist, SQLite runs/events)
FastAPI --kaggle kernels push--> Kaggle GPU batch --HMAC-signed HTTPS POST--> FastAPI /api/events
```

**1. Auth0** (dashboard): Regular Web Application with Universal Login; a Database Connection with *Disable
Sign Ups* on and no social logins; an API with identifier e.g. `https://agent-ops-board-api` (RS256); callback
`http://localhost:8501/oauth2callback` (use your HTTPS URL in production). Create users manually and give them
temporary passwords. Copy each user's id (`auth0|…`) into `allowed_users.json` (see
`allowed_users.example.json`; git-ignored). Set `"enabled": false` to revoke access immediately.

**2. Streamlit**: copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` (git-ignored). The UI is
login-gated whenever an `[auth]` section exists (or `AOB_REQUIRE_LOGIN=1`). Verify the access token's `aud` is the
API identifier: the ID token must never be used for the API.

**3. FastAPI environment**

| Variable | Meaning |
|---|---|
| `AUTH0_DOMAIN`, `AUTH0_AUDIENCE` | Tenant domain (`tenant.us.auth0.com`) and API identifier |
| `AOB_ALLOWED_USERS_FILE` | Whitelist path (default `allowed_users.json`) |
| `AOB_EVENT_SECRET` | Long random secret; per-run signing keys are derived from it |
| `AOB_EVENTS_URL` | Public HTTPS URL of `/api/events` that Kaggle posts to |
| `AOB_DEMO_MIN_INTERVAL`, `AOB_DEMO_MAX_RUNS_PER_DAY` | Per-user start cooldown (300 s) / daily cap (5) |
| `AOB_KAGGLE_NOTEBOOK_DIR`, `AOB_KAGGLE_BIN` | Notebook folder (default `kaggle-notebook/`) and CLI binary |

Kaggle CLI credentials (`~/.kaggle/kaggle.json` or `KAGGLE_USERNAME`/`KAGGLE_KEY`) live only on the API host.

**4. Notebook**: [kaggle-notebook/notebookb061a06458.ipynb](kaggle-notebook/notebookb061a06458.ipynb) holds
Steps 1-6 (clone, deps, restore CUDA `llama-server`, locate Qwen3-4B GGUF, start server + `/health` 200, real
`workers.check_model` + `modelcheck.py`). If you maintain the notebook on Kaggle, `kaggle kernels pull
sillypanda5/notebookb061a06458 -p kaggle-notebook -m` and keep the `aob_emit(...)` calls. Check that Steps 3-4
match your Dataset layouts, and keep `kernel-metadata.json` on the T4 GPU, internet, the two Datasets and
`kernel_sources=[]`, without `docker_image`, saved as UTF-8 without BOM. On each start the launcher stages a copy
whose first cell defines `AOB_RUN_ID`, `AOB_EVENTS_URL` and `aob_emit(event_type, payload)`. Events:
`notebook_started`, `environment_ready`, `model_located`, `model_ready`, `model_validation_started`,
`worker_validation_completed`, `modelcheck_completed`, `model_validation_completed`. Send only counts, timings and
status, never real PII or full prompts/responses. The notebook receives a key valid for its own run only, never the
master secret or any Auth0 credential.

**5. Run and expose**: start the API and UI as above. Keep the API bound to localhost; the reverse proxy should
publish **only** `POST /api/events` (the existing `/v1/*` routes are unauthenticated).

| Endpoint | Auth |
|---|---|
| `GET /api/me` | Auth0 + whitelist (401 / 403 / 200) |
| `POST /api/demo/start` | Auth0 + whitelist; 202 `{run_id, status: "submitting"}`, 409 if a batch is active, 429 when rate-limited |
| `GET /api/demo/runs`, `/api/demo/{run_id}/status`, `/api/demo/{run_id}/events?after_id=&types=` | Auth0 + owner of the run (404 otherwise); `after_id` is the server receive-order cursor |
| `GET /api/demo/{run_id}/state`, `/metrics`, `/summary` | Same; live Workers/GPU/queue snapshot + timeline, time series, final evidence |
| `POST /api/events` | `X-AOB-Timestamp` + `X-AOB-Signature: sha256=HMAC(run_key, "<ts>.<body>")`; deduplicated by `(run_id, event_id)` |

The status response also carries `phase` (`initializing | loading_model | model_ready | validating | validated`,
`null` until an event arrives, never guessed), `model`, `last_event_*` and `result`
(`pending | passed | failed | incomplete`). Kaggle `complete` without the final passing
`model_validation_completed` event is `incomplete`, not a pass.

Run states: `submitting → queued → running → complete`, or `failed` / `cancelled`. Only one batch is active at a
time (enforced in SQLite, so it survives restarts); Kaggle status is polled through the status endpoints, and runs
stuck beyond `AOB_DEMO_MAX_RUN_SECONDS` (3 h) are failed so they cannot block the slot. Kaggle **batch** is not an
always-on service: queue time is not guaranteed and the model is gone once the batch ends; runs and events stay
queryable. Review Kaggle's usage policy and GPU quota before promising anything to a client.

## Worker + GPU autoscaling live demo

`POST /api/demo/start` takes `{"mode": "light" | "high_concurrency", "units": 50 | 100 | 150}` (units only for
`high_concurrency`, default 100; `light` is fixed at 10 tasks / 2 workers / 1 GPU). The launcher adds `AOB_DEMO_MODE` /
`AOB_DEMO_UNITS` to the staged prelude and appends one last cell (marker `# AOB scaling demo bootstrap`) that runs
`scaling.bootstrap.run(globals())` from the cloned repository, **after** the original five validation events. The
earlier cells are not changed, and an existing GPU 0 `llama-server` on `:8080` is adopted if it is isolated on
CUDA 0 (otherwise it is restarted, or the run fails with `GPU0_NOT_ISOLATED`).

What really happens inside the batch: GPU 0 `llama-server :8080` -> inference router `:8090` (workers use
`AOB_MODEL_URL=http://127.0.0.1:8090`) -> SQLite queue + `workers.supervisor` (2 real worker processes) ->
synthetic burst -> `Autoscaler` (single writer of `desired_workers`: 2 -> 4 -> 8 by queue depth, one tier per
decision; back to 2 only after the queue is idle for 15 s) -> GPU 1 `llama-server :8081 --device CUDA1` started
after >= 4 workers and a sustained backlog, joins the router only after its own `/health` is 200, is drained
(in-flight = 0) and stopped when idle. Thresholds live in `scaling/settings.py`.

New events (same envelope, `payload` carries the data): `scaling_demo_started`, `scaling_metrics_snapshot` (every
3 s), `worker_scaling_requested` / `worker_scaling_completed`, `gpu_instance_starting` / `_ready` / `_failed` /
`_draining` / `_stopped`, `inference_route_changed`, `scaling_demo_completed`, `scaling_demo_failed`. Actual workers
are live PIDs with fresh heartbeats; GPU requests are router completion counters. The verdict (`passed | degraded |
failed`) is computed from that evidence (levels 4 and 8 confirmed, requests on both GPUs, GPU 1 stopped cleanly,
final 2 workers + 1 instance) and never from the Kaggle batch status or the model validation result. A missing
measurement is `null`/`N/A`; one-GPU batches report `GPU_UNAVAILABLE` as `degraded`.

Not covered: this is telemetry of a batch, not a customer-facing inference service, and "1 GPU instance" means one
model process, not fewer Kaggle GPUs. Nothing here has been executed on real Kaggle T4s; run High Concurrency with
50/100/150 tasks once and check the evidence table before presenting results.

## Not production

No RBAC beyond the live-demo whitelist, no encryption or retention; the `/v1/*` API is unauthenticated (keep it
on localhost); SQLite as queue; retry intervals are demo values.
