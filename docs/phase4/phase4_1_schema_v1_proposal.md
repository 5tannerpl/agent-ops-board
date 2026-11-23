# Unified Agent Observability Schema v1 (proposal, not implemented)

**Design decision:** one logical read model, not one mandatory ingestion protocol. A Run describes work; a Trace describes causality; a Unit describes a business item; a Task describes scheduled work when such an identity exists. None is a synonym for a Span. The Azure capture needs no new ID to reconstruct its tree. Kaggle already has separate unit, job and attempt values (`AZ/app/benchmark.py:21-33,48-63`; `B/workers/worker.py:95-100`). `AZ` and `B` resolve to the exact checkout paths in the [audit](phase4_1_contract_audit.md).

## Identity, wire types and scope

| Concept | Proposed v1 rule |
|---|---|
| Source | Required nonempty server-owned string. External: resolve credential to `source_id`. Signed Kaggle adapter: namespace the verified run/owner under a server-controlled source; resource `aob.source` is descriptive only. Authorize every read for that source (and run owner where applicable). |
| Run | `(source_id, run_id)`; `aob.run_id` required nonempty string on external spans; signed batch may supply its verified envelope run. Do not require a common producer ID pattern. Zero to many traces may be observed during a run. |
| Trace / span | Native OTLP `trace_id` = 16 nonzero-as-a-whole bytes; `span_id` = 8 nonzero-as-a-whole bytes. API: lowercase hex32/hex16 without `0x`. Span identity `(source_id,trace_id,span_id)`, bound to one run for a given observation. Exact replay is idempotent; conflicting bindings are errors, not successful updates. Enumerate distinct trace IDs per run; do not use trace ID as run PK. |
| Parent | Native `parent_span_id`: empty for a true root, otherwise valid 8 bytes. Look up within source + trace; classify in-run parent, explicitly declared external boundary, or unresolved. Permit late parents across batches. Reject self/cyclic graphs for completeness; never synthesize a parent or make every orphan a root. |
| Unit / worker | `aob.unit_id`, `aob.worker_id`: optional nonempty opaque strings (required on benchmark unit work; worker required on worker work). Scope to run; same worker may process many units, different workers may retry the same unit. |
| Task / attempt | `aob.task_id`: optional string for a real scheduler ID; signed legacy integer -> decimal text in normalized view, retain raw type. Never invent it from `unit_id`. `aob.attempt`: positive integer, required on new retryable unit work, not assumed to be 1 on historical records. |
| Span envelope | Keep native `name`, `kind`, `status.code`, raw `start_time_unix_nano`, `end_time_unix_nano`; expose exact ns as decimal strings to JSON clients where needed. End >= start; compute duration before conversion to display units. Keep safe scope name/version. UNSET is not failure or explicit business success. No duplicate `aob.trace_id`, timestamps or status-code attributes. |

Normative for **new v1** producers; adapt unversioned input explicitly. Do not rewrite legacy IDs that fall outside OTLP widths (`B/trace_store.py:11-14,82-116`). Keep them retrievable under their original signed transport namespace, not silently padded into OTLP identifiers.

## Resource, work and specialized signals

- Require `service.name` for new producers; `service.version`, `deployment.environment.name`, `cloud.provider`, `cloud.platform`, `cloud.region`, and `service.instance.id` optional when known and permitted. These are resource metadata, not tenant IDs. A local Azure sample need not claim `cloud.provider=azure`. Do not label an unknown environment `local` in the normalized view. Keep legacy `aob.environment` as input alias. Retain existing bounded `aob.code_commit` for build provenance if useful; omit unknowns. Keep resource and span attributes distinct.
- Preserve the existing domain keys `aob.workflow`, `aob.dataset_version`, `aob.dataset_seed` (integer), `aob.test_mode` on run work; `aob.task.status` (`COMPLETE`/`FAILED`) on terminal unit work; `aob.validation.passed` (boolean) on executed validations. Unknown/missing is not false. Keep `aob.validation.result` as legacy raw input only: structural `valid` is **not** business validation success. Basis: `AZ/app/benchmark.py:53-85`; `B/otlp_api.py:76-86`; `B/otlp_store.py:36-46,61-68`.
- GenAI profile: use `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` (nonnegative integers when measured). Count actual calls and report measurement coverage; do not convert missing usage into observed zero. Keep Kaggle's trace-local ancestor attribution; mark truly unattributed calls. GPU allocation, scaling, per-device counts and latency remain typed run event/metric series joined by authorized run, not invented span attributes or proxy worker IDs. Basis: `B/trace_store.py:170-225`, `B/workers/otel.py:50-60,87-96`, `B/security/event_schemas.py:191-209,224-239`.
- Bound ID and metadata strings (recommend 128 and 256 characters respectively), enforce per-field types and existing batch limits; reject declared v1 required fields that are invalid rather than silently degrading. Allowlist resource and span keys independently. Exclude tokens, prompts, completions, free-form exception bodies, headers, baggage and arbitrary event/link payloads. Retain raw **safe** names/values, not unrestricted raw protobuf. Put contract version on the read model/manifest (`schema_version: 1`), separate from OTel schema URLs and the existing signed batch envelope version. Unversioned Azure remains a supported input.

### Normalize names at read time; keep originals

`role` below is a derived query field, **not** a new required span attribute. Suggested neutral raw names for new instrumentation appear first where appropriate. Existing names remain valid without dual emission.

| Derived role | Accepted names / rule |
|---|---|
| run; worker | `aob.run`, `aob.azure.run`; `aob.worker`, `aob.azure.worker` |
| unit execution | `aob.unit.task`, `aob.worker.task`; legacy `aob.unit.completed` / `aob.unit.failed` are outcome records, not additional attempts to count twice |
| classify; lookup | `agent.classify`, `aob.agent.classification`; `tool.lookup`, `aob.tool.lookup` |
| validate | `result.validation`, `aob.result.validation` |
| model call | `gen_ai.operation.name=chat` or legacy `chat <model>`; independent of classification role |
| other | Preserve raw safe span and graph without guessing role or outcome |

Current naming evidence: `AZ/app/benchmark.py:48-85`, `B/otlp_store.py:101-110`, `B/trace_store.py:189-196`. Role normalization must not turn one raw span into multiple counted spans.

## Finalization and query contract

1. **Transport:** keep standard OTLP/HTTP success/partial-failure rules and authenticated signed JSON upload as separate adapters. Accepted unique rows, duplicate rows, exported batch lengths and business completion are distinct counters. No new service required (`B/otlp_api.py:93-140`; `B/demo_api.py:213-257`; `AZ/app/telemetry.py:19-29,69-79`).
2. **Manifest:** reuse source-authenticated completion after export with `run_id`, `expected_spans`, `expected_units` (nonnegative integers, actual run expectations, not global constants). Optional explicit boundary declarations identify local entry span, its trace and its external parent. Do not send manifests as spans. Record expectation origin; idempotent matching repeats, conflicting manifests reported explicitly. No manifest means expectations unknown. Legacy signed `final` remains visible as *reported final*, not a retroactive proof of structural completeness.
3. **Graph verdict:** `TRACE_COMPLETE` only when a manifest matches distinct spans/units across **all** traces, with no unresolved internal edges, cycles or identity conflicts. A declared external parent accounts for a boundary, not a valid internal edge. Unexpected late insert invalidates or recomputes the verdict. Separate business state (`COMPLETE`/`FAILED`/unknown) from graph state: a fully observed failed run may have a complete trace set.
4. **Private summary v1:** strings `source_id`, `run_id`, `trace_status`; nullable `environment`; integers `trace_count`, `span_count`, `unique_units`, `worker_count`, `valid_parent_links`, `external_parent_links`, `unresolved_parent_links`; nullable `expected_spans`, `expected_units`. Return `completed_units`, `failed_units`, `unknown_outcome_units` using the latest unambiguous terminal attempt per distinct unit, never the number of ERROR spans. Conflicting terminal records yield unknown, not double counting. Return validation check counts (`validation_passed`, `validation_failed`, `validation_unknown`) separately from number of validation spans. Missing historical outcomes stay unknown. Do not claim successful checks from `status=UNSET`.
5. **Reads:** retain existing summary aliases/legacy routes, add authorized source selection, per-run trace list and per-trace timeline, with stable order and bounded pagination. Keep raw timeline and unit/worker filters. Do not expose another source by accepting an unchecked source parameter. Keep Kaggle owned-run and GenAI coverage views intact. Current limitations: `B/demo_api.py:262-322`, `B/otlp_store.py:71-135`; exact Azure verifier expectations: `AZ/verify_aob.py:36-45`.

**Benchmark acceptance example (proposed result):** one trace, 405 spans, 100 unique units, four workers, 404 valid internal edges, zero unresolved, 100 completed and 100 positive validations. For the saved root without inbound context, external edges = 0. With a declared upstream caller, external edges = 1 and the internal count remains 404. Only the successful fixture has these exact counts.
