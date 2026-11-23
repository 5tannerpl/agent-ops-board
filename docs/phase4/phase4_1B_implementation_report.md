# Phase 4.1-B implementation and verification

**Result:** The actual Azure exporter and AOB private API agree on 405 distinct spans, 404 internal edges, 100 completed units, no failed or unknown units, 100 passed validations, four workers with 25 units each, and no model calls. This was a local SQLite/loopback test, not a deployment.

## What changed

- External OTLP remains protobuf over `/v1/traces`. Resource and span allowlists are separate. IDs, attempts, outcomes, validation booleans, benchmark metadata and measured GenAI usage are type checked. Span kind is the OTel enum number; start/end nanoseconds are exact decimal strings. Display duration is calculated from the integer difference. The raw name and legacy display fields remain available.
- A source still comes from the ingest credential, never a resource label. Replay compares the **retained safe projection**: changing a discarded attribute is not an identity conflict. Exact replay returns the duplicate header, not another successful unit. A conflicting identity rolls back every insert in that batch, leaves original evidence untouched, records the affected run/identity and returns 409. There is no automatic conflict clearance.
- The normalized role is derived at read time. Per-unit outcomes use the latest observed attempt with unambiguous terminal evidence. An unfinished later attempt or conflicting terminal evidence is unknown. Legacy named outcome spans are evidence, not additional executions. OTel `ERROR`/`UNSET` and structural validation labels are not business outcomes. Validation counts are per observed check, not per distinct unit. Unmeasured tokens are not zero.
- Graph checks consider all traces in the authenticated source/run, with parent identities resolved only in the same trace. Cycles, unresolved edges, unmatched external declarations, identity conflicts or count mismatches preclude `TRACE_COMPLETE`. No manifest means `PARTIAL`. Late data recomputes the verdict; a matching repeated manifest is idempotent and a different manifest returns 409.
- Kaggle keeps its signed JSON ingestion, original IDs, owner checks, ancestor correlation, token coverage and GPU events. Its existing owned-run trace summary has an additive `schema_v1` object. The signed final flag is exposed as `reported_final`, not an assertion of OTLP manifest completeness. Integer job IDs remain integers in raw attributes and appear as text in the normalized trace row.

## Database migration and compatibility

The existing SQLite connection initializer checks `PRAGMA table_info` and adds missing columns under a write transaction. No table rebuild, row deletion, synthetic parent, ID rewrite or historical enrichment is performed.

| Table | Additions |
|---|---|
| `external_agent_spans` | Nullable `resource TEXT` (filtered JSON), `start_time_unix_nano TEXT`, `end_time_unix_nano TEXT`, `kind INTEGER`, `scope_name TEXT`, `scope_version TEXT`. The existing attributes JSON retains typed optional task ID, attempt, outcome and validation. Text ns supports the full unsigned wire range without float rounding. |
| `external_agent_completions` | Nullable `external_parents TEXT`, a canonical JSON array of trace/entry/parent triples. A null legacy declaration means no declared boundaries. |
| `external_agent_conflicts` | New table keyed by `(source_id, run_id, trace_id, span_id)`; no raw conflicting payloads stored. |

The span primary key, old columns, indexes and signed tables remain. An old row with no exact envelope cannot certify an exact v1 replay; it stays readable and is not silently overwritten. Legacy `local` without retained resource evidence is ambiguous and reads as null. New records use an empty legacy storage value for unreported environment (the original column is `NOT NULL`) and expose null. No operator migration command or production credential is required. Before rollout, back up the DB; disable producer finalization to stop new manifests. Do not drop columns or treat reverting the reader as reconciliation of conflicts.

## API contract examples

Private reads use the existing verified user credential and enabled whitelist record. Add `"external_sources":["local-python-agent"]` to a user's server-side record to grant that source. Only configured, granted sources are listed. With no explicit grants field, the original single-source installation remains accessible to enabled demo users; with multiple configured sources it grants none. An explicit empty list grants none. An explicitly requested unknown or unauthorized source returns 404. An ingestion token alone fails private authentication.

```text
GET /api/external/sources
=> {"items":[{"source_id":"local-python-agent"}]}
GET /api/external/runs?source_id=local-python-agent
GET /api/external/runs/{run_id}/summary?source_id=local-python-agent
GET /api/external/runs/{run_id}/traces?source_id=local-python-agent&limit=20&offset=0
GET /api/external/runs/{run_id}/traces/{trace_id}?source_id=local-python-agent
GET /api/external/runs/{run_id}/traces/{trace_id}/timeline?source_id=local-python-agent&worker_id=worker-01&unit_id=INT-0001
```

New trace collections have stable ordering and a maximum page size of 1000. Existing run/summary/worker/unit/timeline/unit-span routes remain; legacy lists with added pagination preserve unpaged behavior when `limit` is omitted. `units`/`workers`/`spans` and `completed`/`failed` remain summary aliases. Example **subset** after finalization:

```json
{"schema_version":1,"trace_status":"TRACE_COMPLETE","environment":null,"expected_spans":405,
 "expected_units":100,"trace_count":1,"span_count":405,"unique_units":100,"worker_count":4,
 "completed_units":100,"failed_units":0,"unknown_outcome_units":0,"valid_parent_links":404,
 "external_parent_links":0,"unresolved_parent_links":0,"validation_passed":100,
 "validation_failed":0,"validation_unknown":0,"llm_calls":0,"token_usage":"N/A"}
```

`POST /v1/traces/complete` uses the **source** credential and JSON, e.g. `{"run_id":"example","expected_spans":405,"expected_units":100}`. For an inbound parent add `"external_parents":[{"trace_id":"<32 lowercase hex>","span_id":"<local entry, 16 hex>","parent_span_id":"<remote parent, 16 hex>"}]`. An undeclared missing parent is not a boundary.

## Cross-repository evidence

Base revisions: AOB `c88fe266ebbd6bc41893c7891ca850034c180527` (`runtime_phase1`); connector `f1ca67970cb2e8b8120f154d031f8f51f456d5e0` (`main`), both with uncommitted implementation changes. The sample used its hash-locked test dependencies. Both AOB apps served real HTTP on ephemeral loopback ports over the same disposable DB. The test used distinct producer, ingest and private credentials, local test RSA keys in place of live JWKS discovery, actual JWT validation and the actual private routes. No cloud account, real GPU, production secrets or remote model were needed.

| Run ID | Trace ID | Observed result |
|---|---|---|
| `azure-py-c611113b-a764-4ef6-a98e-0891d0c723ff` | `04ed04cfe36b4b0a3c052d8a1f7b8a77` | No inbound parent. Producer `COMPLETE`; exporter acknowledged 405, zero failed batches; private summary `PARTIAL` before manifest and `TRACE_COMPLETE` afterwards. |
| `azure-py-bfbf285e-7c84-460e-84d6-e9cb758d3276` | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` | Inbound W3C context. Opt-in `AOB_OTLP_FINALIZE=1` submitted the remote boundary after successful flush. Same 404 internal edges, plus one declared external edge. |

For **each** run, the separate sample verifier passed against the private HTTP API. The integration test independently joined timeline IDs, checked the run/worker/unit ancestry and classify/lookup/validate siblings, checked the four worker partitions, then replayed the original protobuf batches (0 inserts, 405 duplicates). A changed name under the same identity returned HTTP 409; the summary changed to `PARTIAL`. The original saved console fixture independently passed `verify_capture.py` (trace `185aea7dffdf657ef348fa097337e86e`); it was not used as a substitute for HTTP verification.

## Tests and reproduction

- B1/B2 gate before private API implementation: **12 passed**. Subsequent receiver tests also cover malformed input and cross-run rebinding.
- Final combined AOB affected suites: **111 passed**, one Starlette test-client deprecation warning. Includes source isolation using identical IDs in two sources, retry/unknown outcomes, old DB, manifests, late parents, cycles, both HTTP runs, signed Kaggle traces and GPU/scaling regressions (simulated hardware). Not a run of every unrelated AOB test.
- Full Azure sample suite: **10 passed**. Both worktrees passed `git diff --check`.
- An intermediate run using `PYTHONPATH` alone had 110 passes and one worker-export failure: the supervisor replaces `PYTHONPATH`, so its child could not import the optional SDK. Repeating with `opentelemetry-sdk==1.45.2` installed in the disposable AOB interpreter yielded the 111-pass result; no worker code was changed to pass the test.

With sibling checkouts, use a disposable AOB test environment including the worker SDK and a separate Azure environment installed from `samples/azure-python-agent/requirements-test.lock`:

```sh
# AOB root; opt-in live tests are skipped if the sample interpreter is not supplied.
AOB_AZURE_TEST_PYTHON=/absolute/path/to/azure-venv/bin/python python -m pytest -q \
  tests/test_otlp_receiver.py tests/test_external_reads.py tests/test_azure_otlp_live.py \
  tests/test_trace_observability.py tests/test_worker_otel.py tests/test_event_contract.py \
  tests/test_scaling.py tests/test_demo_scaling.py tests/test_demo_api.py tests/test_live_demo_panel.py
# Azure sample directory, using that sample's interpreter:
python -m pytest -q
python verify_capture.py evidence/spans.jsonl.gz evidence/sample-run.json
```

## Affected files

- AOB implementation: `otlp_api.py`, `otlp_store.py`, `otlp_semantics.py` (new), `demo_api.py`, `trace_store.py`.
- AOB tests: `tests/test_otlp_receiver.py`, `tests/test_external_reads.py` (new), `tests/test_azure_otlp_live.py` (new), `tests/test_trace_observability.py`.
- AOB documentation: `docs/phase4/phase4_1B_implementation_report.md` (this file). The four existing Phase 4.1-A files were read but not modified.
- Azure, all under `samples/azure-python-agent/`: `app/benchmark.py`, `app/models.py`, `app/telemetry.py`, `verify_aob.py`, `tests/test_verify_aob.py`, `tests/test_transport.py`, `README.md`.

No deployment, production infrastructure or secret change, commit or push was performed.
