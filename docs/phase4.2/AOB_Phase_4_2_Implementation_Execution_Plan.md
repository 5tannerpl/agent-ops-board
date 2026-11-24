# AOB Phase 4.2 — Implementation Execution Plan (post-G0)

**Basis:** `discovery.md` (2026-11-24), `AOB_Phase_4.2_Technical_Design_v2.md`, `AOB_Product_Roadmap_Phase_4.2-4.7_v2.md`.

**Status:** G0 source discovery completed; recommended ADRs accepted as implementation baseline for development only. No production, Azure, Kaggle, RunPod, or OCI changes made by this plan. No budget approval. The user must authorize paid RunPod testing separately.

## 0. Non-negotiable existing behavior

- Current AOB: `runtime_phase1` at `c0cbfef1f5f4cc6cb5d738f5d6a36ff925b30770` as inspected by discovery, not verified against a live checkout here.
- Azure sibling checkout: `aob-cloud-connectors`, `samples/azure-python-agent`, `main` at `f1ca679` with working tree changes. Preserve its deterministic default.
- Do not replace `/api/demo/start`, `/api/demo/runs`, the owned run read routes, `/api/external/*`, signed `/api/events` and `/api/traces`, or `/v1/traces` + `/v1/traces/complete`.
- Do not mount `/v1/dashboard` on the private production API or public ingress: it belongs to the dev app.
- Do not change legacy Kaggle semantics, notebook routing, telemetry evidence, or historical run ownership.
- Read paths must never call Kaggle CLI, Azure Agent, RunPod, inference endpoint, resource-management API, or model-health probe.
- Registering external/private resources is metadata-only, requires no image/GHCR, GPU, cloud key, management adapter, or network probe.
- Observe-only is the default; it cannot start an Agent, invoke inference, or delete external resources.
- 4.2 reserves future Hybrid and MCP contracts; it does not implement connectors or MCP processes.

## 1. First: branch, evidence and baseline

1. Create `feature/phase4-2-crosscloud` from the verified target branch. Capture `git status`, commit and tracked changes. Do not overwrite user working-tree edits.
2. Confirm active OCI Compose project, routes, volumes, image digests and TLS via scoped read-only inventory; confirm Azure Container App resource, deployed revision/image digest, run URL/auth and a staging target. Do not dump secret values.
3. Fix the five known `tests/test_staging_config.py` failures by explicitly distinguishing isolated staging internal HTTP from public production HTTPS. Do **not** relax production validation.
4. Execute AOB and Azure sample test suites; record *new* actual counts, not old counts as a new run. Historical G0 baseline: AOB 142 pass / 5 fail; Azure sample 10 pass / 0 fail.
5. Add default-off `AOB_CROSS_CLOUD_ENABLED=false`, distinct execution approval and hard-coded allowable destination policy, without enabling cloud requests.

**Gate G0.1:** staging regression green; live deployments and rollback inputs inventoried; no paid actions, no secret exposures.

## 2. Phase 4.2A — registry + policy + query split

### Storage (additive schema only)

Add namespaced tables without altering historic rows:

- `deployments`: `deployment_id` PK; `tenant_id`, `owner_id`, `provider`, `runtime_type` (`agent`/`inference`), `ownership` (`external` default, `managed`); `connection_mode` (`public_https` / `private_connector` / `push_only`), `execution_policy` (`observe_only` default / `execute`), `status`, `capabilities_json`, `config_version`, nullable `image_digest`, `provider_resource_id`, `endpoint_ref`, `credential_ref`, `connector_ref`, `telemetry_policy_ref`, timestamps. Never store plaintext keys or secret-bearing URLs.
- `deployment_bindings`: historical immutable per-run references: local run ID and namespace, tenant, owner, `agent_deployment_id`, `inference_deployment_id`, their config versions, source, remote run ID, workload version, deadlines, business outcome, telemetry status, cost observation provenance; use unique scoped identities, not unqualified run IDs.
- `operations`: `operation_id`, tenant, `operation_type`, `idempotency_key`, request hash, resource ID, outcome, timestamps, unique `(tenant_id, operation_type, idempotency_key)`. Different request hash with same key -> 409.
- `run_dispatch`: stable local run ID, remote ID (allocated before start), state (`reserved`, `dispatching`, `accepted`, `uncertain`, `terminal`), attempts, last safe error, deadline. Must be durable before remote side effects.

For legacy rows, preserve old readers and avoid fabricating tenant/source ownership. Reuse `demo_store` WAL transaction and indexed lookup patterns; test restoration on a **copy** of DB.

### Interfaces: four independent contracts

- `DeploymentProvider` (optional): inspect/capabilities and later explicitly authorized provision/delete; not used for Agent run or inference calls. No generic mandatory cloud management client.
- `AgentRunClient`: start/status, capability-gated cancel. Kaggle and Azure clients differ.
- `InferenceProvider`: call inside Agent execution trust domain, not through OCI; return usage and safe status.
- `TelemetryProvider`: optional provider-reported snapshot/health; authenticated OTLP push remains separate and does not require pull provider.

### Shared services and security

- `DeploymentRegistryService`: registration/lookup/unregister of owned metadata; external unregister never calls cloud delete.
- `RunCommandService`: authorized admission, slots/budget, durable dispatch and reconciliation. Never imply GPU-ready after accepted dispatch.
- `RunQueryService` and `TraceQueryService`: bounded stored-only queries, principal/resource/source scoped *before* SQL counting/pagination; source IDs and remote run IDs always namespaced; provenance and `as_of` included.
- `AuthorizationPolicy`: transport-independent tenant/principal/scope/resource-grant resolution; preserve legacy whitelist compatibility only for old routes. Never use client-provided owner claims.
- `CapabilityResolver`: effective = advertised capabilities ∩ ownership ∩ execution policy ∩ principal grant ∩ permitted reachability.

Candidate routes (do not replace legacy): `POST /api/v1/deployments`, `GET /api/v1/deployments/{id}`, `POST /api/v1/deployments/{id}/validate` (configuration-only), `POST /api/v1/runs` (202 and idempotency), `GET /api/v1/runs/{id}`, `GET /api/v1/runs/{id}/telemetry` (bounded), and `POST /api/v1/runs/{id}/cancel` (only if available; otherwise structured NOT_SUPPORTED). Mount on **private** app only. No routes on public ingress for reading or management.

**Tests:** two tenants, two source IDs with colliding remote run IDs, cross-tenant 404, bad audience/expiry 401, missing scope 403, cursor/pagination, read does zero network calls, no content/credential leaks; unknown GPU/cost -> N/A; external metadata registration with no adapter/endpoint contact; unregister/cancel no provider delete. Concurrent same idempotency key = one reservation; different payload -> 409. Verify old rows still readable.

**Gate G1:** registry and isolation tests pass, Kaggle/OTLP interfaces unchanged, backups/restore rehearsal passes.

## 3. Phase 4.2B — AgentRunClient & Azure baseline

**AOB repo**: wrap existing `demo_service` -> `kaggle_launcher.submit` and kernel status behind `KaggleAgentRunClient` retaining existing behavior. Add `AzureAgentRunClient` for exactly the verified Azure API:

- `POST /demo/runs` with strict `external_run_id="azure-py-<UUID>"`, `task_count=100`, `concurrency=4` and run bearer.
- `GET /demo/runs/{R}` and optional `GET /demo/runs/{R}/units`; 200 completed, 202 busy/running, 409 conflict, 503 capacity, 504 timeout. Do not infer completion from HTTP acknowledgement alone.
- Generate and persist `external_run_id` **before** transmission. On timeout or restart mark `uncertain`, GET status and reconcile before any retry. Since Azure history is memory-only, an unknown GET after restart cannot prove execution did not happen: do not blindly replay a potentially billable request.
- Only `/api/v1/runs` with explicit authorized execute permission selects Azure; original `/api/demo/start` remains Kaggle. For observe-only/unknown client, report NOT_SUPPORTED.
- Azure Agent currently lacks cancel; return capability NOT_SUPPORTED and stop further local dispatch without claiming remote cancellation.

**Azure sibling repo**: maintain 100 synthetic tasks / 4 workers deterministic default. Optional inference client behind opt-in config only; do not turn it on before G3 approved provider smoke. Do not rewrite `/demo/runs` request schema or 60-second sample contract without a versioned change plan. In particular, a cold GPU startup that exceeds the 60-second sample timeout needs explicit deadline policy before integration.

**Tests:** replay, conflicting idempotency, timeout after remote accepted, service restart, token rotation, local history and failed/unknown result mapping. Keep zero-model-call deterministic trace baseline separate from future LLM evidence.

**Gate G2:** 100-unit deterministic Azure sample and Kaggle mocks pass, no secrets in telemetry; no unauthorized inference.

## 4. Phase 4.2C–E — RunPod LB + Cached Models PoC (requires separate budget approval)

Before any provider creation, verify current official RunPod LB endpoint, cached model, health and pricing docs against current account. Confirm supported GPU architecture, region, active workers/min workers, idle timeout, image-pull billing, Replica metrics API, token semantics and budget. Owner must approve actual provider account, a dedicated endpoint, testing period and capped monetary estimate.

- Separate inference worker image from Azure Agent. Pin build commit and image digest; CUDA compatibility and non-root ability verified on real GPU. No model/keys baked in.
- Pin Qwen3-4B-Instruct-2507 Q8_0 by verified HF repo, revision, exact filename, checksum; discovery did **not** verify these. Cache resolver must select exact file under `/runpod-volume/huggingface-cache/...`, fail closed on mismatch and report model load errors.
- LB ingress must expose validated `/ping` and authorized OpenAI-compatible `/v1/chat/completions`; internal llama-server on loopback. Check LB header propagation and real readiness. Never probe inference to check idle status.
- Initial max workers=1, min/active=0 if supported. Test cold and warm 3 times each; measure arrival, ready time, tokens/s, client latency, cache, actual replicas and separate provider bill. Only then consider 2 replicas for load demonstration.
- If LB/cache/trace headers are unsuitable, stop and document Queue-based or Network Volume alternatives; do not silently switch model or engine.

**Gate G3:** real inference returns from pinned model with verified GPU offload, actual bill/evidence recorded; idle-to-zero status verified independently or documented as unknown. No invented savings/cost figure.

## 5. Phase 4.2F/G — Azure Agent -> RunPod -> OCI OTLP

- Enable inference only for the approved staging Azure Agent with dedicated inference credential, allowlisted HTTPS host and safe request limits (tasks, in-flight calls, output tokens, retries, wall-clock timeout).
- Keep deterministic run mode as default; inference mode must be opt-in. Use fixed synthetic email workload and stable Unit IDs. For each Unit record its final outcome and one or more actual inference attempts. 100 units is not necessarily exactly 100 calls if retries; report attempted/succeeded/failed calls distinctly.
- Propagate W3C `traceparent` from AOB -> Azure -> LB gateway when verified; if LB strips it, use a validated correlation reference and explicitly label links as correlation, not proven parent-child trace edges. No sensitive prompt/completion/body in spans or errors.
- Export Agent OTLP to OCI source-bound bearer ingress. GPU gateway OTLP only if supported and authorized; otherwise provide provider-reported metrics with freshness/provenance. Don't conflate Azure Agent worker count, inference slots and active GPU replicas.
- Treat business outcomes, trace completeness and GPU lifecycle as independent dimensions. Validate duplicate and late spans, manifest counts, unresolved edges, and partial telemetry. Don't call completed run success unless unit validation meets criteria.
- Investigate the 60-second Azure sample execution timeout against real cold-start P95; introduce bounded configurable timeout in a backward-compatible manner before the first end-to-end 100-unit test. A proposed initial 20-minute run deadline is an operator setting, not proof of affordability.

**Gate G4:** 100 real Unit terminal results with counted LLM attempts; no lost identities, no double start, Kaggle and Azure-only unchanged.

**Gate G5:** at least 10 sampled cross-cloud trace/correlation checks, complete coverage report for 100 units, lost/late/repeated telemetry tests, provider worker metrics vs observed samples with accurate unknowns.

## 6. Phase 4.2H — Dashboard, cost governance, staging release

- Keep Streamlit dashboard, add provider/runtime selector and summary of Azure Agent + inference endpoint without building separate React/BFF. Add measured LLM unit timeline; GPU/cold-start/replica panels only when actual evidence exists. `N/A` not `0` for missing data.
- Feature flag disabled by default; explicit execute grant/whitelist, single concurrent demo, daily/monthly estimated budget reservation, task/token/GPU limits, deadline on server, no blind retries on ambiguous network results.
- Explicit user confirmation is necessary for a paid test. Estimated budget is not a hard provider billing cap; reconcile against real invoice. Do not mix public account tokens with source OTLP tokens.
- Staging first: independent SQLite backup + corresponding files and configuration snapshots; additive migrations; previous OCI image/Compose and Azure revision pinned; execute security/compat tests and ARM64 smoke. Roll back via flag/revision, not destructive table drops.
- No external resource deletion on Run completion, cancellation or unregister; only a separately authorized managed, dedicated resource may be deleted with exact resource ownership proof.

**Gate G6:** release evidence (schema/API docs, changed files, test matrix, real trace and provider metrics, invoice/redacted receipts, actual deployment digests, canary, rollback outcome) and clear remaining limitations. No production-ready claim from demo alone.

## 7. Copilot implementation instruction (paste as one task)

Implement **Phase 4.2A only** on a new development branch, using `docs/phase4.2/discovery.md` and Phase 4.2 v2 design. Start by inspecting current repository; do not assume file layout from the design. First repair the five staging-config test regressions without relaxing production HTTPS policy. Then implement additive deployment/run/idempotency schema, transport-independent authorization and capability resolution, registered external metadata with no cloud calls, stored-only scoped read service, and private route compatibility adapters. Preserve current Kaggle, signed events/traces, Azure OTLP and old REST contracts. Write tests for cross-tenant/source ID collisions, two-principal authorization, read-no-side-effects, unknown metric semantics, observe-only deny, external unregister no delete, idempotency race/conflict, migration/restore. Do not implement RunPod, paid operations, Hybrid connector, MCP, arbitrary image execution or deployment on OCI. Report changed files, diff, actual tests, tests failed, open risks, and stop at G1 for review. Each completed substep should be committed separately; never commit keys or secrets. If staging baseline cannot be made green, stop and report exact error rather than bypass it.

## 8. Source references

- `discovery.md`: baseline and gaps, API contracts, policy/read risks, proposed minimal changes, tests 142/5 and 10/0, gates and ADRs.
- `AOB_Phase_4.2_Technical_Design_v2.md`: §§1–9 architecture, PoC, trace, budget and acceptance; §§10–11 Copilot and reserved Hybrid/MCP boundaries.
- `AOB_Product_Roadmap_Phase_4.2-4.7_v2.md`: 4.2A–H and 4.2.1/4.2.2 scoping.
