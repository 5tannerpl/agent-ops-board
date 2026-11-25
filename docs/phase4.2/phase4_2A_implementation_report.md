# Phase 4.2A: local G1 implementation report

**Decision: G1 passed locally; no deployment approval implied.** External Agent and inference configurations can now be registered and validated without resolving an address, installing an adapter, provisioning a resource or starting a run. Registry metadata describes intent, not proof of connectivity, cloud ownership or available GPU capacity.

## Starting point and baseline

`feature/phase4-2-crosscloud` started clean at `13b251af578b5cdbe41ce4aee250644920893a1d`. The changes since the [discovery baseline](discovery.md) were documentation. The Azure sample was tested at sibling checkout `f1ca679` with pre-existing local changes; this implementation did not edit that checkout.

The startup validator applied the production HTTPS prefix/suffix check in staging, while the launcher required exact equality to the isolated ingress. Consequently the intended HTTP URL failed, three bad URLs had the wrong diagnostic and an HTTPS substitute was accepted. Commit `dae535f` changes staging only to require exactly `http://event-ingress:8011/api/events`. Production's HTTPS rule, TLS behavior and both environments' authentication/file requirements are unchanged. No failing tests were skipped or weakened.

| Executed on x86_64, CPython 3.12.3 | Pass | Fail | Skip |
|---|---:|---:|---:|
| Original AOB suite | 142 | 5 | 0 |
| AOB after isolated staging fix, before registry | 147 | 0 | 0 |
| Final AOB suite including Phase 4.2A tests | 150 | 0 | 0 |
| Azure sample suite, before and after (each run) | 10 | 0 | 0 |
| Legacy fixture generation at `c88fe266` | 1 | 0 | 0 |

Separate 3.12 environments used `requirements.lock` and the sample's hash-checked `requirements-test.lock`; both passed `pip check`. AOB full runs used a temporary HOME/runtime, empty inherited environment, `AOB_AZURE_TEST_PYTHON` pointing to the sample interpreter, and `python -m pytest -q -ra -p no:cacheprovider --basetemp=<fresh-path>`. One Starlette TestClient deprecation warning persisted. During focused test development an incomplete synthetic OTLP row lacked nullable transport columns; the fixture was corrected, not the ingestion code.

## Implementation and file inventory

| Files | Change |
|---|---|
| `security/production_config.py` | Staging startup policy fix. |
| `crosscloud/policy.py`, `crosscloud/history.py`, `demo_api.py` | Shared source-grant calculation and owner-qualified stored legacy lookup; only legacy callers retain implicit single-source grants and active-run reconciliation. |
| `crosscloud/store.py`, `crosscloud/registry.py` | Additive schema, read-only/writer connections, atomic receipts, typed configuration, registration and local unregister. |
| `crosscloud/providers.py`, `crosscloud/commands.py` | Four independent structural contracts, admission checks, observed-run bindings and local lifecycle commands. |
| `crosscloud/queries.py` | Six stored-only query boundaries. No resolver, scheduler, model or provider handle is supplied. |
| `api/deployments.py`, `api/private_app.py`, `config.py`, `Dockerfile` | Authenticated private transport, startup flag/migration and inclusion of the new Python modules in the existing image build. No Compose or live deployment changes. |
| `tests/test_crosscloud.py`, `tests/rehearse_phase4_migration.py` | Isolation, restart, duplicate admission, data minimization, zero-network and legacy backup/restore coverage. |
| This report | Local evidence, API contract and remaining gates. |

### Schema and idempotency

Private startup with the flag enabled initializes the existing demo database and executes one additive transaction. No historic record is backfilled or granted to an inferred owner. Flag-off requires no new tables; disabling it after use leaves history intact.

* `deployments`: stable local ID, Auth0 subject as owner scope, provider/role, ownership, connection mode, execution policy, source, endpoint/credential/image/resource/connector/policy references, digest, capabilities, version, status and timestamps. External/observe-only by default. Configuration is immutable in this phase; unregister tombstones the registration. Register a new configuration under a new ID.
* `deployment_bindings`: stable local ID, owner, unique `(owner_id, source_id, remote_run_id)`, agent and optional inference IDs with pinned versions, workload version, deadline, cancellation intent, business status and separate telemetry status.
* `operations`: owner, operation type, hashed idempotency key and canonical request hash, stored result, operation ID and timestamp. Under `BEGIN IMMEDIATE`, same key/body returns the original receipt; different body conflicts. An interrupted, uncommitted local action rolls back with its receipt. No network effect can occur inside this transaction.
* `run_dispatch`: reserved schema for a future dispatcher. Registration and external observations create **no** dispatch row. There is no replay worker, remote execution, management action or GPU-release logic.

### Provider and service boundaries

`DeploymentProvider` defines capability advertisement, inspection, provisioning and deletion. `AgentRunClient` defines start and status, with a separate optional cancellation protocol. `InferenceProvider.generate(system, user, timeout)` is the existing worker contract's inference subset. `TelemetryProvider` defines optional read-only health and time-window metrics. None is required for registration, and OTLP push does not depend on them.

`RunCommandService` can store observations, status or cancellation intent. An observe-only start is denied; even an eligible execute configuration gets `NOT_SUPPORTED` in this phase. A recorded cancellation explicitly reports that remote cancellation was not performed. No lifecycle command accepts a deletion adapter.

`RunQueryService.list_runs` and `get_run_summary` include owned legacy history without polling; other projections target explicitly bound source/run pairs. `get_run_trace` returns IDs, parent and duration, not arbitrary span names/attributes. `get_failed_tasks` uses existing outcome semantics and returns hashed unit labels. `get_llm_metrics` reports observed usage, not implied cost or proof of complete coverage. `get_gpu_metrics` reports unavailable/null for bound runs; it does not probe. These services use SQLite `mode=ro`. No MCP process or endpoint was added.

### Private contracts and authorization

`AOB_CROSS_CLOUD_ENABLED` defaults to `false`. Restart with `true` to mount the routes on the private app. Flag-off or public telemetry ingress: 404. Switching it on does **not** enable remote requests. Do not enable it in production on the strength of this report.

| Route | Request / result |
|---|---|
| `POST /api/v1/deployments` | Requires `Idempotency-Key` (1-128 characters), provider and runtime type. Returns 201 with the recorded deployment, including on identical replay; different payload with same key returns 409. |
| `GET /api/v1/deployments` | Returns `{items: [...]}`; default 50, limit 1-100, offset 0-10000. Owner and current source grants filter before paging. |
| `GET /api/v1/deployments/{id}` | Returns scoped metadata, including registration status. Inaccessible and absent IDs both return 404. |
| `POST /api/v1/deployments/{id}/validate` | Revalidates registered metadata; returns `configuration_valid: true`, `reachability: "unknown"`. No DNS, connection, inference or provisioning. |

Example request body for a pre-existing LLM: `{"provider":"existing","runtime_type":"inference","connection_mode":"public_https","endpoint_ref":"ref:approved-llm"}`. References are operator aliases, not URLs or secret values. They are not resolved in 4.2A. No image, GPU allocation or provider key is needed. A private endpoint uses `private_connector` and an alias; this does not assert the existence of a connector.

Auth0 RS256 issuer/audience/expiry/subject verification remains in place. New routes use the verified subject, enabled allowlist record and intersection of JWT `scope` with the operator's `cross_cloud_scopes` list. Register requires `aob:deployments:write`; get/list/validate require `aob:deployments:read`. A supplied source must also appear in both `external_sources` and the server's read catalog. There is no client-supplied owner or implicit single-source permission. Missing or invalid authentication is 401; missing scope is 403. Input validation errors omit submitted values. Run services separately require runs read/write/execute, traces read or metrics read. No run command API is published.

## G1 acceptance and limits

- [x] External Agent/private metadata and external LLM registrations without a deployment adapter, image, cloud key or rental. Private API tests deny DNS and socket connections.
- [x] Verified JWT negatives, explicit grants, revoked sources, owner isolation, collision of identical remote IDs across sources, authorization before pagination, bounded responses and sanitized failures.
- [x] Sixteen duplicate registrations on independent connections produced one deployment and one receipt; fresh interpreter replay returned the same ID. Changed payload conflicted; another owner had an independent key space.
- [x] Observe-only denied; unsupported remote start/cancel explicit. Local failure, unregister and cancellation leave external infrastructure untouched and historical versions readable. No dispatch adapter exists.
- [x] Read-only history: denied network and reconciliation hooks, unchanged SQLite data version, missing GPU metrics null, missing usage not invented.
- [x] Offline migration/restart/restore rehearsal passed on a populated disposable pre-upgrade database. Checked old row projections, signed traces, external spans, old reader, restored backup and SQLite integrity.
- [x] Kaggle mocks, signed event/trace contracts and existing Dashboard/REST tests passed. Both Azure loopback cases completed 100 deterministic tasks across four workers with 405 spans, 404 internal parent links and zero LLM calls. The separate checked-in Azure capture verifier also passed. Neither result verifies a live cloud deployment.

**Limits and release gates:** owner-scoped rather than organization tenancy; immutable version 1 (no in-place config update); no secret or destination resolution; no unbound OTLP discovery via these new services; no GPU provider measurements. Use legacy routes for unbound external history or detailed Kaggle telemetry. Bound outcome/usage projections reject more than 2,000 spans rather than silently computing a partial aggregate. There is no dispatch reconciliation, paid execution grant, budget enforcement or MCP transport. The local gate does not establish OCI ARM64 image compatibility, proxy isolation on the deployed host, mounted-volume backup suitability, Azure deployed revision/authentication, or a Kaggle GPU/RunPod smoke. Those remain operator verification gates. No live cloud actions were taken.

**Next step, after separate approval:** for 4.2B first verify the actual Azure staging resource, revision/digest, run contract and rollback inputs, and the OCI staging routes, volumes, backup and ARM64 image. Then wrap existing Kaggle submission without changing its semantics and test deterministic Azure dispatch against a local fake, including an ambiguous timeout after acceptance. Do not blindly retry or enable inference. Work stopped at local G1.
