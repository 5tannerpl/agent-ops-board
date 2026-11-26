# Phase 4.2B-UI — unified workspace

**Scope:** the eight-page cross-cloud dashboard, not completion of Phase 4.2. The [discovery report](phase4.2/dashboard_discovery.md) precedes the implementation. No production or OCI deployment, provider traffic change, inference or GPU allocation was performed.

## Implemented

The existing cross-cloud flag selects an explicit Streamlit navigation tree. With it off, existing local and legacy demo workflows remain available under their original restrictions. The new workspace does not register legacy pages. The presentation environment label does not alter the production login gate.

| Change | Purpose |
|---|---|
| `dashboard/app.py`, new `dashboard/workspace.py` | Eight pages grouped under Workspace, Agent Operations, Observability and Agent Governance. Selection stores a local ID, not an authorisation grant. |
| `dashboard/azure_run.py` | Same reservation, same-key recovery and stored-status polling; readable status and automatic run selection. |
| `dashboard/api_client.py`, `api/deployments.py`, `crosscloud/queries.py` | Private binding history, registry display projection, source aggregate and binding-scoped evidence. No database migration or new public route. |
| `dashboard/live_demo.py`, new `dashboard/trace_table.py` | Shared span-table presentation; preserve the legacy external browser. |
| `dashboard/auth_gate.py`, `deploy/oci/compose.crosscloud-staging.yaml` | Status-only account verification errors; identify staging without relaxing production authentication. No port, volume, credential or network changes. |
| `tests/test_dashboard_login.py`, `tests/test_crosscloud.py`, `tests/test_remote_dispatch.py` | Selection, receipt recovery, source collisions, scope/owner denial, registry field projection and binding history without telemetry. |
| `README.md`, this report, discovery and screenshots | Workspace entry point, evidence and staging handoff. |

### Private API compatibility

Existing command, registry, per-run summary, legacy external and ingress contracts remain compatible. New reads require the enabled allowlist record and the intersection of signed JWT and allowlist scopes. The configured source catalog further restricts source grants.

* `GET /api/v1/deployments?workspace=true` uses `aob:deployments:read` and returns `{items}` with display fields only. Credential, endpoint and image references are excluded. The old projection remains unchanged by default.
* `GET /api/v1/runs` uses `aob:runs:read` and returns `{total,items}`. Includes authorised workspace bindings without dispatch or spans; excludes the legacy provider. Filters: local/remote ID substring, exact provider, business outcome, deployment ID, inclusive `since`/exclusive `until` UTC epoch seconds, limit/offset. Each item has binding, source and deployment IDs, provider, workload, business status, stored telemetry, creation time and nullable observed span interval. Without `aob:traces:read` the interval is not disclosed.
* `GET /api/v1/observations/count` uses `aob:traces:read` and counts spans across **explicitly granted sources**, including unbound observations. This is not a per-run count.
* `GET /api/v1/runs/{local}/llm-metrics` reuses the existing bound metrics service and requires runs-read and `aob:metrics:read`. It distinguishes no evidence (`null`) from measured zero observed calls. Trace-only reads do not include this metrics projection.
* `GET /api/v1/runs/{local}/observations/{summary|units|workers|timeline}` requires both runs-read and traces-read. The server first resolves the owned/source-granted binding, then queries **its** source and remote ID. The caller cannot override either identity. Summary is an object; detail is `{items}`. Timeline supports unit filter and bounded pagination. Summary, units and workers reject evidence sets over 2,000 spans rather than presenting truncated aggregates. The existing paging policy allows up to 100 rows/request and offset 10,000.

### What the evidence means

The fixed Azure configuration reflects the actual adapter contract; the UI does not edit task count or workers. The dispatcher accepts remote `complete` only with 100 completed and zero failed for that contract. Intermediate task counts are not persisted. Task outcomes, worker intervals and trace relationships on Agent Observability are **observations**; the configured concurrency is not a worker count. Duration in history is the interval covered by received spans, not a provider invoice or a guaranteed wall-clock execution time.

The sibling Azure sample attempts `/v1/traces/complete` only for OTLP with `AOB_OTLP_FINALIZE=1` after successful delivery/flush. The receiver verifies the source manifest against unique units, span count, conflicts and parent graph (including external parent boundaries). Without that marker, a successful export can still have `PARTIAL` derived evidence while the binding remains `unknown`. Nothing here rewrites binding telemetry. Historical absence of markers does not establish which producer setting, deployment revision or network hop was responsible.

## Implemented and verified locally

| Verification | Result |
|---|---|
| Full AOB suite, Python 3.12 with `requirements.lock`, separate sample interpreter from its hash-checked test lock, `AOB_AZURE_TEST_PYTHON` set, `python -m pytest -q -ra` | **166 passed, zero skips, zero failures**. One FastAPI TestClient deprecation warning. |
| End-to-end deterministic execution | Actual loopback sample, lost POST response, status-only recovery. One remote POST; terminal business success. New private history and evidence reads confirmed the local/source/remote association, **100 completed, 0 failed, 4 observed workers, 405 spans, 0 observed LLM calls**. Stored telemetry `unknown` and manifest-derived `TRACE_COMPLETE` coexisted. Focused integration test also rerun with these exact assertions: passed. These numbers belong only to the local test run. |
| Security regressions (included above) | Anonymous/invalid token, missing scopes, other owner, revoked source, non-approved execution target and public-ingress reads rejected. Same remote ID in two sources returned different, correct evidence. Registry workspace response excluded operator references. Lost-receipt recovery retained the same operation key. |
| Browser | All eight implemented pages rendered in Chromium. Registry → filtered history → Agent → LLM → Infrastructure preserved the selected binding. A 390-pixel viewport check found no document-level horizontal overflow on Infrastructure. Browser capture used a local test identity, not a live Auth0 login. |
| Packaging | Local `linux/amd64` Docker build and disposable four-service staging Compose smoke passed (flag off/on, isolated mounts/networks, no public worker port, clean worker shutdown). Tested local image ID `sha256:9f9972d4cea2d0692e2c7d713111e665fe5db295b1eacffb93305506288c9c10`; **not an OCI ARM64 release image**. |

## Local screenshots

These are renders of the implemented pages, **not** the Word mockups or OCI screenshots. The data is from a real local execution of the deterministic Azure sample using an isolated test database, a test identity and loopback services. The capture session used a read-only token and had remote starts disabled.

| Workspace / operations | Observability | Governance |
|---|---|---|
| [Overview](phase4.2/ui-screenshots/1-overview.png) | [Agent](phase4.2/ui-screenshots/4-agent.png) | [Agent Registry](phase4.2/ui-screenshots/7-agent-registry.png) |
| [Execution](phase4.2/ui-screenshots/2-execution.png) | [LLM](phase4.2/ui-screenshots/5-llm.png) | [Resource Registry](phase4.2/ui-screenshots/8-resource-registry.png) |
| [Run History](phase4.2/ui-screenshots/3-run-history.png) | [Infrastructure](phase4.2/ui-screenshots/6-infrastructure.png) | |

## Controlled staging deployment (not executed)

1. Use the [existing B.1 staging runbook](phase4.2/phase4_2B1_deployment_readiness_report.md) for approved Azure revision/target verification, network egress enforcement, image architecture/digest, secret mounts, backup and recovery. Record the currently running image, flags and dispatch state. Do not replace a dispatcher while work is in flight or uncertain. Do not use the production project or volume.
2. Build and review the candidate for the OCI host architecture. Load the approved immutable image on the staging host. Set `AOB_STAGING_IMAGE` in the protected **staging-only** environment file. The same image is required for dashboard and private API. Keep `AOB_CROSS_CLOUD_ENABLED=true`, `AOB_REMOTE_RUNS_ENABLED=false` for initial read-only validation; explicitly stop/drain any previously enabled staging dispatcher before this transition.
3. Verify staging Auth0 audience and allowlist. For the designated reader grant only the intended `external_sources` and page-specific scopes (`aob:deployments:read`, `aob:runs:read`, `aob:traces:read`, `aob:metrics:read`) in **both** token and allowlist. Request a fresh login. Do not broaden an existing user's grants automatically. The dashboard still runs with `AOB_ENV=production`, without a DB mount. Keep private reads off the ingress proxy.
4. From the repository root, on the approved staging host only:

   ```sh
   docker compose --env-file "$STAGE_ENV" -p aob-crosscloud-staging \
     -f deploy/oci/compose.crosscloud-staging.yaml up -d --no-build --wait control-api event-ingress dashboard
   ```

5. Through the authenticated staging UI verify all eight pages, scoped registry, historical no-span bindings, a selected source/remote ID, and stored versus derived status. Compare actual counts with authorised records, not with historical constants. Test denial with a restricted test account. No execution is needed for the read-only gate.
6. Only with separate authorisation, follow the B.1 runbook to enable the already-approved Azure target, execute grant and **one** dispatcher. Start once, recover an ambiguous receipt with the **same** operation key, and follow the local ID through History → Agent → LLM → Infrastructure. Record dispatch state, attempts, business result and source-scoped evidence separately. Do not start from another tab/session to resolve uncertainty.

### Rollback

Block new starts, follow the operator drain/reconciliation procedure, and preserve the staging DB and evidence. Never delete a lock or edit dispatch/telemetry state to force progress. Reapply the recorded previous image digest and flags to the same standalone staging project, initially with dispatch off, and recreate the services using the command above. Verify login, private reads, ingress health and historical records before any separately approved resumption. No UI-specific downgrade migration is required. Do not run `down -v`. If the stored state itself needs recovery, use the verified backup/restore procedure from B.1, **not** a DB rewind to reissue a remote start.

## Implemented but not verified in staging / remaining blockers

Live Auth0 grants, actual OCI architecture/image promotion, historical source mappings and current Azure reachability need a controlled staging window. The supplied 810 spans were an aggregate at a point in time, not a promised total for any run. A matching source/remote binding is required to show detail; unbound spans are not guessed into a run.

The registry does not persist friendly names or region. The approved built-in target has a display label; other names derive from provider and deployment identity. There are no infrastructure collectors, verified RunPod connection, billing rates, measured real-time replicas, latency percentiles or general model inventory. Unsupported values stay unavailable, not zero. Registration is not a health check. Select an older run via History beyond the recent-run selector. Existing evidence/paging limits apply; no new retention policy or background polling of providers was introduced.

Next product implementation remains **4.2C/D/E**: RunPod Load Balancing Endpoint, llama.cpp runtime and Cached Models integration.
