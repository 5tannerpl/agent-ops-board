# Phase 4.1-C release readiness — 2026-11-23

**Decision: HOLD.** Both native images build, the old-database rehearsal passes, and the Azure benchmark passes through the private API. Two gates are still open: five pre-existing AOB staging failures, and no successful execution of the release images on ARM64. Neither a green benchmark nor the availability of ARM64 wheels closes those gates. No deployment, infrastructure or production secret change, commit, push, or image publish was performed.

| Gate | Result |
|---|---|
| Full AOB suite, CPython 3.12.3, Linux x86_64 | **142 passed, 5 failed**, no skips, 58.65 s. One Starlette `httpx` test-client deprecation warning. |
| Full Azure Python Agent suite, separate hash-locked interpreter | **10 passed**, 4.58 s. `pip check` clean in both environments. |
| Real Azure OTLP to real AOB HTTP and authenticated private read | Both cases passed inside the full suite. Producer `COMPLETE`, 405 spans acknowledged and persisted, 404 internal links, 100 completed, 0 failed/unknown, 100 passed validations, 4 x 25 units, 0 LLM calls. No manifest: `PARTIAL`; matching manifest: `TRACE_COMPLETE`. Inbound-parent case: 1 declared external edge. Exact replay adds 0; conflicting replay returns 409. Saved capture also verified independently. |
| Existing features | Signed Kaggle trace ingestion, deduplication, owner checks, measured LLM input/output and ancestry, optional worker console export, GPU/scaling and dashboard tests passed. Signed trace rows and summary gained additive fields in B; not a claim of byte-identical JSON. Hardware, remote model, real Auth0 tenant and Kaggle submission were not exercised. |
| Old SQLite DB -> migration -> restart -> restore | Passed on a populated pre-change test DB made by the baseline app. Original columns/rows compared across all tables; `integrity_check=ok`. Not a production backup or production-size timing test. |
| Native Docker | AOB and Azure `linux/amd64` builds and offline UID 10001 smokes passed. No ports exposed on the host. |
| ARM64 | Both unmodified release build recipes resolved ARM base layers but failed at their first `RUN`: `exec /bin/sh: exec format error`. Existing builder advertises only amd64 variants. CPython 3.12 Bookworm-compatible binary distributions found for all Linux runtime lock entries (64 AOB, 27 Azure; Azure hashes verified). **Not an ARM64 runtime pass.** No host emulator installed. |

## Review scope and changes

Compared tracked, staged and untracked files against AOB `c88fe266ebbd6bc41893c7891ca850034c180527` (`runtime_phase1`) and connector `f1ca67970cb2e8b8120f154d031f8f51f456d5e0` (`main`). No staged changes or unrelated pending source edits identified. The four untracked Phase A documents are audit inputs, not new C implementation. The tracked Windows `.venv-lock` is baseline material, not a pending dependency update.

- AOB B implementation: `demo_api.py`, `otlp_api.py`, `otlp_store.py`, `otlp_semantics.py`, `trace_store.py`; `tests/test_otlp_receiver.py`, `tests/test_trace_observability.py`, `tests/test_external_reads.py`, `tests/test_azure_otlp_live.py`. The [B report](phase4_1B_implementation_report.md) records migration and API contracts. The other existing documents here are `phase4_1_alignment_matrix.md`, `phase4_1_contract_audit.md`, `phase4_1_implementation_plan.md`, `phase4_1_schema_v1_proposal.md`.
- AOB C: `requirements.txt`, `requirements.lock` now include the worker SDK rather than relying on a manually provisioned interpreter. Lock regenerated with pip-tools 7.7.0 for the image's Python 3.12; Windows-only colorama pin retained. OTLP receiver uses `opentelemetry-proto==1.45.2`; SDK and API are 1.45.2, semantic conventions 0.66b2. Kaggle's `requirements-otel.txt` already pins API and SDK. AOB doesn't need the network exporter. Lock pins versions, not artifact hashes.
- `Dockerfile` now copies the missing `otlp_semantics.py` and imports both apps plus SDK during build. `.dockerignore` excludes the legacy lock environment and local isolated secrets directory.
- `config.py`, `demo_api.py`, `otlp_api.py`, `deploy/oci/compose.isolated.yaml`, `deploy/oci/.env.isolated.example`, `tests/test_external_reads.py`: separate non-secret private source catalog from ingress credentials; reject ambiguous duplicate credential values, malformed maps and non-ASCII bearer input. Split-role test reads both sources without an ingress token map in the reader. `tests/rehearse_phase4_migration.py` and this report are new.
- Azure changes all under `samples/azure-python-agent/`: `README.md`, `app/{benchmark,models,telemetry}.py`, `verify_aob.py`, `tests/{test_transport,test_verify_aob}.py` (B); `.env.example` (C: finalization default). No lock churn needed: `requirements.in` pins API/SDK/OTLP HTTP exporter 1.45.2; `requirements.lock` and `requirements-test.lock` pin and hash those plus transitive proto/transport dependencies. Docker installs runtime lock only.

## Gates, boundaries and operator actions

1. **Resolve staging policy before sign-off.** Running `tests/test_staging_config.py` in a clean archive of the AOB base produced the identical five failures (5 passed). `test_staging_requires_all_authentication_inputs` rejects the documented isolated HTTP endpoint even when authentication inputs are present. All four cases of `test_staging_rejects_non_isolated_event_urls` fail: three reject with the production URL policy/message and one accepts HTTPS where the staging policy expects rejection. The guard applies the production HTTPS rule to staging; the staging launcher and documentation expect exactly `http://event-ingress:8011/api/events`. Agree on the staging contract, reconcile the guard and rerun the full suite; do not relax production TLS or authentication to get a pass. No staging-policy change is bundled here.
2. **Rebuild and smoke both release images on an authorized native ARM64 or already-emulating builder**, without publishing; record image IDs, architecture, UID and health/import results. Static checks cannot establish native ABI compatibility. AOB's base tag is mutable: promote the tested digest rather than silently rebuilding at deployment.
3. **Provision grants explicitly.** Isolated control API receives `AOB_OTLP_READ_SOURCES` (JSON list of IDs; defaults `[]`), never the ingestion token map. Keep catalog equal to intended ingress source IDs and use explicit `external_sources` on every enabled whitelist entry, including `[]` for no access. A single configured source with *absent* grants deliberately retains legacy access for enabled users; with multiple sources there is no implicit grant. Catalog mismatch can hide a source or expose historical data to a granted reader. Legacy combined-process config falls back to token-map keys if catalog is unset. The older non-isolated Compose recipe is not configured or certified for this external pipeline.
4. **Keep credential boundaries separate.** Private reads verify RS256 signature, issuer, audience, expiry, subject and enabled server-side whitelist. Source-scoped SQL and denied-source 404s were exercised with colliding IDs; an ingest bearer alone returned 401. Ingress derives identity from its configured bearer, not producer labels; Kaggle signed JSON uses its existing HMAC path. Keep per-source high-entropy secrets distinct from producer run tokens, HMAC keys and read credentials. The isolated template still injects ingress tokens as environment: restrict Docker/host access, never log rendered real Compose config, and don't use checked-in placeholder values. Use TLS outside allowed local endpoints.
5. **Finalization defaults off.** `AOB_OTLP_FINALIZE=1` is for the AOB `/v1/traces/complete` endpoint only, after successful flush/delivery. The manifest POST uses the source credential, bounded timeout, no redirects or environment proxy; failure to confirm is not proof of failed business execution. Keep producer endpoint/token environment fixed over process lifetime (finalization rereads it); restart for rotation. Read back independently. Unresolved edges, missing counts and conflicts preclude completeness. There is no durable producer spool; do not assume automatic recovery.

## Reproduction commands

Run locally with sibling checkouts; paths below are shell variables for the exact commands (temporary root in this run: `/tmp/aob-41c.3i11WPId`). Use examples only, never production credentials.

```bash
cd /workspaces/agent-ops-board
A=$PWD; Z=/workspaces/aob-cloud-connectors/samples/azure-python-agent
T=$(mktemp -d /tmp/aob-41c.XXXXXXXX)
for r in "$A" "$Z"; do git -C "$r" status -buall --short; git -C "$r" diff HEAD; git -C "$r" diff --cached --stat; git -C "$r" diff --check; done
# Lock generation used virtualenv + pip-tools 7.7.0:
# pip-compile --no-emit-index-url --no-emit-trusted-host --output-file=requirements.lock requirements.txt
# Linux generation omits the Windows-only pin; retain that marker for Windows.
virtualenv -p /usr/bin/python3.12 "$T/aob"; virtualenv -p /usr/bin/python3.12 "$T/azure"
"$T/aob/bin/pip" install -r requirements.lock
(cd "$Z" && "$T/azure/bin/pip" install --require-hashes -r requirements-test.lock)
"$T/aob/bin/pip" check; "$T/azure/bin/pip" check
AOB_RUNTIME_DIR="$T/runtime" AOB_AZURE_TEST_PYTHON="$T/azure/bin/python" "$T/aob/bin/python" -m pytest -q -ra --basetemp="$T/aob-tests"
(cd "$Z" && "$T/azure/bin/python" -m pytest -q --basetemp="$T/azure-tests" && "$T/azure/bin/python" verify_capture.py evidence/spans.jsonl.gz evidence/sample-run.json)

mkdir "$T/legacy"; git archive c88fe266 | tar -x -C "$T/legacy"
(cd "$T/legacy" && "$T/aob/bin/python" -m pytest -q tests/test_staging_config.py --basetemp="$T/base-staging")
(cd "$T/legacy" && "$T/aob/bin/python" -m pytest -q tests/test_trace_observability.py::test_trace_ingestion_is_signed_bounded_and_idempotent --basetemp="$T/base-tests")
"$T/aob/bin/python" tests/rehearse_phase4_migration.py "$T/legacy" "$T/base-tests/test_trace_ingestion_is_signed0/demo.db"

docker compose --env-file deploy/oci/.env.isolated.example -f deploy/oci/compose.isolated.yaml config --format json |
  python -c 'import json,sys;s=json.load(sys.stdin)["services"];assert json.loads(s["control-api"]["environment"]["AOB_OTLP_READ_SOURCES"])==["local-smoke"] and "AOB_OTLP_SOURCE_TOKENS" not in s["control-api"]["environment"] and "AOB_OTLP_SOURCE_TOKENS" in s["event-ingress"]["environment"]'
docker buildx inspect default
docker buildx build --platform linux/amd64 --load -t aob-41c:3i11wpid .
(cd "$Z" && docker buildx build --platform linux/amd64 --load -t aob-41c-azure:3i11wpid .)
docker run --rm --network none -e AOB_DEMO_DB_PATH=/tmp/demo.db aob-41c:3i11wpid python -c 'import os,streamlit; from api.private_app import healthz as a; from api.public_ingress import healthz as b; assert os.getuid()==10001 and a()==b()=={"status":"ok"}'
docker run --rm --network none aob-41c-azure:3i11wpid python -c 'import os,secrets;os.environ["RUN_API_TOKEN"]=secrets.token_urlsafe();from app.main import create_app;assert create_app() and os.getuid()==10001'
docker buildx build --platform linux/arm64 --load -t aob-41c:arm64-3i11wpid .
(cd "$Z" && docker buildx build --platform linux/arm64 --load -t aob-41c-azure:arm64-3i11wpid .)
P=(); for n in $(seq 36 -1 17); do P+=(--platform manylinux_2_${n}_aarch64); done; P+=(--platform manylinux2014_aarch64)
"$T/aob/bin/pip" download --only-binary=:all: --no-deps --python-version 3.12 --implementation cp "${P[@]}" -r requirements.lock -d "$T/arm-wheels"
(cd "$Z" && "$T/azure/bin/pip" download --require-hashes --only-binary=:all: --no-deps --python-version 3.12 --implementation cp "${P[@]}" -r requirements.lock -d "$T/az-arm-wheels")
```

## Migration and rollback

No live database was supplied. The rehearsal began with an existing on-disk database from a separate baseline signed API run (one owned run, one batch, three spans including a measured LLM call). It opened the original **read-only**, used SQLite backup, then added a legacy external span and completion with the old writer on the disposable copy. The new initializer ran twice in independent processes. Six nullable envelope columns, nullable `external_parents`, and the conflicts table were added without rewriting legacy evidence. Every original row projection matched; legacy envelope columns remained null. The old reader opened both the upgraded database and a restored pre-migration snapshot. This is read/restore compatibility, not permission to mix old and new writers.

For a later authorized release:

1. Record both previous image digests and non-secret configuration versions. Quiesce all writers/finalizers; take SQLite online backups of the relevant databases, verify `PRAGMA integrity_check` and rehearse on a backup at representative volume. Do not copy a live WAL-mode `.db` alone.
2. If rollback is needed, stop producers, ingress, control and any other volume users. Disable producer finalization and preserve a separate post-upgrade backup for investigation/reconciliation.
3. Restore the verified pre-upgrade backup to a clean volume/path with correct ownership and **no stale WAL/SHM sidecars**. Restore previous config and immutable image digests together. Do not drop additive columns, clear conflicts or run the old writer alongside the new one. Restoring a snapshot loses later writes unless separately retained and reconciled.
4. Before reopening traffic, check integrity, both health endpoints, authenticated owned Kaggle trace/LLM summary and expected source authorization; confirm a wrong credential is denied. Resume producers only after approval. Never interpret HTTP acknowledgment alone as a successful private read-back.
