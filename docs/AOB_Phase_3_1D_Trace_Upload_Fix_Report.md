# AOB Phase 3.1D — Trace Upload Fix Report

## Scope

This change fixes the Phase 3.1 post-run trace uploader for the OpenTelemetry SDK Console Exporter JSON emitted by the real Kaggle worker run:

```text
Run: run-1dc64a06f0be27d8
Reported source spans: 308
Task spans: 100
LLM spans: 100
Validation spans: 100
Observed upload result: TRACE UPLOAD: complete 0
Observed local persistence: zero trace_batches and trace_spans rows
```

The fix does not change GPU scaling, worker orchestration, model validation, Auth0, or the existing `/api/events` contract.

## Root cause

The uploader expected these identifiers at the top level:

```python
raw["trace_id"]
raw["span_id"]
```

The OTel Console Exporter places them in:

```python
raw["context"]["trace_id"]
raw["context"]["span_id"]
```

The exported identifiers also use `0x` prefixes. As a result, every span was discarded before batching and the uploader incorrectly reported completion with zero spans.

## Implemented fixes

### Console Exporter identifier extraction

The generated uploader now:

- Reads `trace_id` and `span_id` from `raw["context"]`.
- Reads `parent_id` from the span record.
- Removes `0x` prefixes from Trace ID, Span ID, and Parent Span ID.
- Preserves names, timestamps, attributes, resources, and status values.
- Keeps the existing sensitive attribute allowlists.
- Keeps the existing per-run HMAC signing mechanism.

### Explicit empty-upload failure

The uploader now fails explicitly when:

- `supervisor.log` is missing.
- No valid spans can be parsed.
- The ingestion response is not a valid accepted/duplicate response.
- The HTTP status is outside the 2xx range.

It no longer reports:

```text
TRACE UPLOAD: complete 0
```

for an empty parse result.

### Response accounting

Each response is parsed and validated. Upload output includes:

```text
TRACE UPLOAD: batch <n> accepted <count> duplicates <count> rejected <count>
TRACE UPLOAD: complete <total> accepted <count> duplicates <count> rejected <count>
```

### Restaging cleanup

Subsequent notebook staging now removes existing cells beginning with:

```text
# AOB_TRACE_BATCH_UPLOAD
```

This prevents duplicate upload cells after repeated Kaggle notebook pulls and pushes.

### Timestamp compatibility

The persistence layer now accepts OTel Unix nanosecond timestamps and normalizes them for duration and time-axis calculations while retaining the original span timing semantics.

## Files changed

- [`kaggle_launcher.py`](E:/AllProjects/2026EmailIntentAI/agent-ops-board/kaggle_launcher.py)
- [`trace_store.py`](E:/AllProjects/2026EmailIntentAI/agent-ops-board/trace_store.py)
- [`tests/test_trace_upload_staging.py`](E:/AllProjects/2026EmailIntentAI/agent-ops-board/tests/test_trace_upload_staging.py)
- [`docs/AOB_Phase_3_1D_Trace_Upload_Fix_Report.md`](E:/AllProjects/2026EmailIntentAI/agent-ops-board/docs/AOB_Phase_3_1D_Trace_Upload_Fix_Report.md)

## Automated verification

The new tests use the actual Console Exporter structure with:

```json
{
  "context": {
    "trace_id": "0x...",
    "span_id": "0x..."
  },
  "parent_id": "0x...",
  "name": "chat test-model",
  "start_time": 1700000000000000000,
  "end_time": 1700000001000000000,
  "attributes": {},
  "resource": {
    "attributes": {}
  },
  "status": {
    "status_code": "UNSET"
  }
}
```

The test fixture verifies:

- 308 spans parse successfully.
- 100 LLM spans are identified.
- Batches split into 200 and 108 spans.
- `0x` prefixes are removed.
- LLM calls retain the correct Unit ID, Worker ID, Attempt, and Parent ID.
- Empty and malformed upload behavior is represented in the generated uploader.
- Previously staged upload cells are removed.

Focused regression result:

```text
70 passed, 3 skipped, 1 warning
```

Included suites:

- Demo API
- Live dashboard panel
- Worker OTel behavior
- Event contract
- Trace upload staging
- Trace ingestion/correlation

## Real Kaggle acceptance status

Real acceptance is **not claimed by this report**.

The fix is locally tested, but a new Kaggle execution is still required. The previously reported run demonstrated the source telemetry but did not successfully ingest it into AOB.

## Required real-run verification

After deploying the new commit and starting one new 100-unit high-concurrency run:

1. Confirm Kaggle output contains:

   ```text
   TRACE CHECK: PASSED
   Worker Task Spans: 100
   LLM Inference Spans: 100
   Validation Spans: 100
   ```

2. Confirm upload output contains accepted batches, for example:

   ```text
   TRACE UPLOAD: batch 0 accepted 200 duplicates 0 rejected 0
   TRACE UPLOAD: batch 1 accepted 108 duplicates 0 rejected 0
   TRACE UPLOAD: complete 308 accepted 308 duplicates 0 rejected 0
   ```

3. Query:

   ```text
   GET /api/demo/{run_id}/trace-summary
   ```

4. Confirm:

   ```text
   llm_calls == 100
   unique_units == 100
   unattributed_calls == 0
   finalized == true
   ```

5. Query:

   ```text
   GET /api/demo/{run_id}/llm-calls?limit=250&sort=start
   ```

6. Compare every call's Unit ID, Worker ID, Attempt, Trace ID, Span ID, timestamps, duration, and tokens with the raw `supervisor.log`.

7. Confirm the Dashboard displays all 100 real calls.

8. Switch between **GPU & Worker Ops** and **LLM Observability** and confirm no second Run ID or Kaggle submission is created.

## Remaining operational limitation

Trace retention cleanup is still not implemented. The current code stops accepting new uploads after the run acceptance window but does not delete old `trace_batches` or `trace_spans` rows. This remains separate from the uploader fix and should be addressed before treating retention requirements as complete.

