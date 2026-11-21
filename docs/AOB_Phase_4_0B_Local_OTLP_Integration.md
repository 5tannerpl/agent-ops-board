# AOB Phase 4.0B - Local OTLP Integration

## Local startup

From the repository root, use two PowerShell windows:

```powershell
$env:AOB_ENV = "development"
$env:AOB_OTLP_SOURCE_TOKENS = '{"local-agent":"replace-with-a-local-secret"}'
$env:AOB_DEMO_DB_PATH = "$PWD\runtime\demo.db"
python -m uvicorn api.public_ingress:app --host 127.0.0.1 --port 8000
```

```powershell
$env:AOB_API_URL = "http://127.0.0.1:8000"
streamlit run dashboard\app.py --server.port 8501
```

The private query API is normally run separately:

```powershell
python -m uvicorn api.private_app:app --host 127.0.0.1 --port 8001
```

No Azure service, collector, or Tempo instance is required.

## Independent Python Agent contract

The agent sends `POST http://127.0.0.1:8000/v1/traces` with:

- `Content-Type: application/x-protobuf`
- `Authorization: Bearer <token>` where the token is configured in the board's
  `AOB_OTLP_SOURCE_TOKENS` JSON mapping. The server derives `source_id`; the
  agent must not attempt to provide or override it.
- A standard `ExportTraceServiceRequest` protobuf from
  `opentelemetry-proto`.

Every span must include the string span attribute `aob.run_id`. For the dashboard
aggregation, application spans should use these additional attributes:
`aob.unit_id`, `aob.worker_id`, and optionally `aob.validation.result`.
Resource attributes may include `deployment.environment.name`, `service.name`,
and `service.version`. Unknown attributes are ignored; sensitive attributes are
never persisted.

The receiver accepts spans in any order and retries are idempotent on
`(source_id, trace_id, span_id)`. A child received before its parent is stored
with `validation_result=missing_parent` and is reconciled when the parent arrives.

After all batches have been sent, the agent must authenticate another request:

```http
POST /v1/traces/complete
Authorization: Bearer <same source token>
Content-Type: application/json
```

```json
{"run_id":"agent-run-123","expected_spans":405,"expected_units":100}
```

The board reports `TRACE_COMPLETE` only if the exact unique span count and unit
count match and every parent is present. A missing or partial batch remains
`PARTIAL`; business completion must not be used as a telemetry completion signal.

The receiver's successful response is an empty standard OTLP
`ExportTraceServiceResponse` protobuf. Accepted and duplicate counts are exposed
in `X-AOB-Accepted-Spans` and `X-AOB-Duplicate-Spans` response headers.

## Local validation

The repository's regression checks can be run with:

```powershell
python -m pytest -q
```

An end-to-end integration is not considered complete until an independently
running Python Agent has sent real OTLP protobuf data and the data is visible in
the private external-run queries and Streamlit view.
