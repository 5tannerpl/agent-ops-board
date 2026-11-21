# AOB Phase 3.1D — Real Kaggle Acceptance Runbook

This runbook verifies the Phase 3.1 unified GPU/Worker and LLM Observability flow using one real 100-unit Kaggle execution.

Do not mark acceptance complete from local fixtures, notebook screenshots, or synthetic spans. Acceptance requires both:

1. Evidence from the real Kaggle session.
2. Matching results from the AOB trace query APIs.

## 1. Release and branch verification

Run from the repository root:

```powershell
git rev-parse HEAD
git rev-parse origin/OPENTELE
git ls-remote origin refs/heads/OPENTELE
```

The expected released commit is:

```text
0c004fd63a964de294c43ba28ad3ea8eaf44c4db
```

The local HEAD, local `origin/OPENTELE`, and remote `OPENTELE` must all resolve to this SHA before launching Kaggle.

## 2. Local startup

The local launcher starts the Public Ingress, Private Control API, Cloudflare Quick Tunnel, and Streamlit Dashboard.

```powershell
.\.venv\Scripts\python.exe .\start_aob_local.py `
  --env-file .\.env.local `
  --cloudflared C:\path\to\cloudflared.exe
```

Expected local endpoints:

```text
Public Ingress: http://127.0.0.1:8011
Private API:    http://127.0.0.1:8010
Dashboard:      http://127.0.0.1:8501
```

Health checks:

```powershell
curl.exe http://127.0.0.1:8011/healthz
curl.exe http://127.0.0.1:8010/healthz
```

The launcher prints configuration names and masks sensitive values. Do not print the complete environment or secret file contents.

## 3. Required configuration names

Set these names in the process environment or private `.env.local` file. Never commit the file or disclose secret values.

### Production and API settings

```text
AOB_ENV=production
AOB_AUTH0_DOMAIN
AOB_AUTH0_AUDIENCE
AOB_ALLOWED_USERS_FILE
AOB_EVENT_SECRET_FILE
AOB_EVENTS_URL
AOB_DEMO_DB_PATH
AOB_API_URL
```

The launcher generates `AOB_EVENTS_URL` from the Cloudflare Quick Tunnel:

```text
https://<tunnel-host>/api/events
```

### Kaggle and tracing settings

```text
AOB_TRACE_URL
AOB_OTEL_ENABLED=true
AOB_OTEL_EXPORTER=console
AOB_OTEL_SERVICE_NAME
AOB_OTEL_ENVIRONMENT
AOB_OTEL_COMMIT
AOB_REPO_BRANCH=OPENTELE
AOB_REPO_URL
AOB_KAGGLE_NOTEBOOK_REF
```

If `AOB_TRACE_URL` is empty, the launcher derives it from `AOB_EVENTS_URL`:

```text
https://<tunnel-host>/api/traces
```

For traced execution, `AOB_OTEL_COMMIT` must be the full 40-character release SHA.

## 4. Ingress boundaries

Scaling event reporting remains separate from trace ingestion.

### Existing scaling event route

```text
POST /api/events
```

### Trace batch route

```text
POST /api/traces
```

Trace batches use the existing per-run HMAC signing boundary:

```text
Content-Type: application/json
X-AOB-Timestamp: <unix timestamp>
X-AOB-Signature: sha256=<signature>
```

The trace body must contain:

```json
{
  "run_id": "run-...",
  "batch_id": "trace-0",
  "schema_version": 1,
  "final": false,
  "spans": []
}
```

The implementation limits trace uploads to:

```text
512 KiB per request
250 spans per batch
32 batches per run
```

Only allowlisted trace metadata is accepted. Prompts, model responses, customer data, cookies, headers, credentials, and arbitrary attributes must not be uploaded.

## 5. Starting exactly one real run

1. Open the Dashboard.
2. Confirm the Dashboard is using the released backend.
3. Confirm no run is active.
4. Select:

   ```text
   Mode: high_concurrency
   Units: 100
   ```

5. Click **Start Live Demo** once.
6. Record the single returned `run_id`.
7. Do not click Start again while the run is queued, running, or being finalized.

The shared Run selector and Start Live Demo controls must remain outside the two internal tabs.

## 6. Kaggle evidence requirements

Wait for the Kaggle session to complete. The console output must contain evidence equivalent to:

```text
SCALING DEMO RESULT: passed
TRACE CHECK: PASSED
Worker Task Spans: 100
LLM Inference Spans: 100
Validation Spans: 100
```

The expected baseline also contains:

```text
Worker Root Spans: 8
```

The trace uploader must report:

```text
TRACE UPLOAD: batch ...
TRACE UPLOAD: complete ...
```

The final non-empty upload batch must contain:

```json
{
  "final": true
}
```

A scaling pass does not prove trace ingestion passed. A trace upload failure must be reported independently and must not rewrite a successful scaling result.

## 7. API verification

Use an Auth0 access token for the selected run owner. Do not place tokens in source control, logs, screenshots, or this document.

### Run status

```powershell
curl.exe `
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" `
  http://127.0.0.1:8010/api/demo/<RUN_ID>/status
```

### Trace summary

```powershell
curl.exe `
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" `
  http://127.0.0.1:8010/api/demo/<RUN_ID>/trace-summary
```

For the verified 100-unit baseline, confirm:

```text
llm_calls = 100
unique_units = 100
unattributed_calls = 0
task_spans = 100
validation_spans = 100
finalized = true
```

Also record:

```text
models
input_tokens
output_tokens
input_token_coverage
output_token_coverage
llm_avg_ms
llm_p50_ms
llm_p95_ms
accepted_spans
duplicate_spans
rejected_spans
```

Missing token usage must remain visible as incomplete coverage; it must not be converted to zero.

### Complete LLM call list

```powershell
curl.exe `
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" `
  "http://127.0.0.1:8010/api/demo/<RUN_ID>/llm-calls?limit=250&sort=start"
```

Every call must contain or resolve to:

```text
unit_id
worker_id
attempt
trace_id
span_id
start_utc
end_utc
duration_ms
status
```

Verify that:

- Every returned LLM Span is present.
- Every correlated call has the correct Unit ID.
- Calls are not matched by call order or timestamps.
- Multiple calls for one Unit remain separate.
- Retry and repair attempts retain their individual Span IDs.
- Unattributed calls are explicitly marked rather than guessed.

### Individual Unit trace

```powershell
curl.exe `
  -H "Authorization: Bearer <AUTH0_ACCESS_TOKEN>" `
  http://127.0.0.1:8010/api/demo/<RUN_ID>/units/<UNIT_ID>/trace
```

Verify the returned tree includes the available real spans:

```text
aob.run
  aob.worker.task
    chat <model>
    aob.result.validation
```

If workflow step records were not uploaded, the response and Dashboard must state that workflow step timings are unavailable. Do not infer them from OTel timestamps.

## 8. Dashboard verification

On the selected Run, verify the Dashboard has exactly these internal tabs:

```text
GPU & Worker Ops
LLM Observability
```

### GPU & Worker Ops

Confirm the existing behavior remains available:

- Scaling status
- GPU state and utilization
- Worker state
- Live metrics
- Scaling event timeline
- Validation events
- Run summary

### LLM Observability

Confirm the page displays:

- Model name
- LLM call count
- Unique Unit count
- Input/output token totals
- Token coverage
- Average, P50, and P95 LLM inference latency
- Task status separately from validation status
- Trace ingestion status
- All 100 real calls, not only the first page or top 10
- Unit ID on every timeline row
- Worker ID and Attempt
- Trace ID and Span ID
- Start/end timestamps
- Duration
- Token values where present

The timeline must use real Span start and end times. Concurrent calls must overlap instead of being rendered sequentially.

Select several calls and compare their values with the raw Kaggle `supervisor.log`.

## 9. Tab-switching verification

With the same Run selected:

1. Record the current Run ID.
2. Open **GPU & Worker Ops**.
3. Open **LLM Observability**.
4. Switch between the tabs repeatedly.
5. Confirm the Run ID remains unchanged.
6. Confirm no second Kaggle submission occurs.
7. Confirm no second Run is created.
8. Confirm the Dashboard does not call:

   ```text
   POST /api/demo/start
   ```

Changing the Run selector may query a different existing Run, but switching tabs must only perform read operations.

## 10. Security and negative checks

Verify the following return an error:

- Trace request without a signature.
- Trace request signed for another Run ID.
- Expired trace timestamp.
- Unknown Run ID.
- Wrong user querying a Run.
- Oversized request body.
- More than 250 spans in one batch.
- Disallowed attribute names.
- Prompt or response-like fields.
- Invalid Trace ID, Span ID, parent ID, or timestamp.

Verify duplicate behavior:

- Reposting the same `batch_id` is idempotent.
- Reposting the same Span ID does not increase accepted span count.

## 11. Retention status

The current implementation prevents new uploads after the run acceptance window expires, but it does not currently delete old rows from:

```text
trace_batches
trace_spans
```

There is no database cleanup job or `DELETE FROM trace_*` retention process in the current implementation. Therefore, retention enforcement is a remaining operational blocker and must not be described as complete.

## 12. Acceptance decision

Accept Phase 3.1D only if all of the following are true:

- The real Kaggle run reports scaling success.
- Kaggle reports 100 task, 100 LLM, and 100 validation spans.
- The expected worker root span count is confirmed.
- Trace upload reports completion.
- The final batch is accepted and finalized.
- `/trace-summary` reports 100 LLM calls.
- `/trace-summary` reports 100 unique Units.
- `/trace-summary` reports zero unattributed calls.
- API records match raw telemetry.
- All Dashboard timeline calls are accessible and correctly correlated.
- Tab switching does not create another run or submission.
- Security negative checks pass.

Until a real Kaggle run and matching API results exist, report the implementation as **ready for acceptance**, not accepted.

