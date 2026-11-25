# Phase 4.2B.1: OCI-to-Azure staging handoff

**Local readiness verified; live acceptance withheld.** No cloud CLI, OCI deployment, Azure traffic change, remote workload, inference or GPU rental was performed. The new Compose file is standalone. Do not layer it onto production.

## What was tested

Clean `feature/phase4-2-crosscloud` at `06ac46d7a05e19a2015036cc939bd215c499271e`, including G1 `70c2921` and tested G2 `c9b995b`. Tested B.1 implementation: **`bd0ede50453471deed45a67aa7710c31f142b4bf`**. This report follows separately. The sibling sample remained `f1ca679` with the eight pre-existing modified files described in the [G2 report](phase4_2B_implementation_report.md); local tests do not attest a deployed image.

| Executed on local Linux amd64 | Result |
|---|---|
| Clean starting AOB suite / Azure sample | 161 / 10 passed |
| Final AOB suite / Azure sample | **164 / 10 passed**, zero skips or failures |
| Populated pre-upgrade fixture (`c88fe266`) | 1 generator test; migration, restart, old projections, old reader and restore passed |
| Populated G1 fixture (`70c2921`) | 1 generator test; SQLite backup, schema applied twice, identical rows in all 12 prior tables, no dispatch contexts, integrity `ok` |
| Dockerfile build and actual four-service staging smoke | 1 standalone smoke passed; `linux/amd64` local image ID `sha256:e16c0a3e930737825565fa78d36dd9f5f70e37d86a24653f440d2176fa059901`. **Not an ARM64 release image.** |

Two separate CPython 3.12 environments, locked AOB and hash-checked sample dependencies, both `pip check` clean. Full tests used empty inherited environments, temporary HOME/runtime, `AOB_AZURE_TEST_PYTHON` set, and `python -m pytest -q -ra -p no:cacheprovider --basetemp=<new-directory>` in each checkout. Existing AOB TestClient deprecation warning. The clean baseline ran from a source archive with the actual sibling sample next to it.

The Compose smoke (`python tests/smoke_crosscloud_compose.py <locally-built-candidate>`) uses its own random project, fresh volume, fake secrets, ephemeral loopback ports, local socket and no approved provider ID. It starts the real API without registry tables, enables registry only, verifies migration against the same volume, starts the worker explicitly with execution off (clean exit), then tests the enabled but empty queue, UID, mounts, networks, port bindings and TERM exit. It deletes **only its own** project/volume. The authenticated reservation and fault paths were tested separately against disposable SQLite: lost response from the actual loopback sample (one POST then GET), worker death and status-only restart against a fake, SIGTERM **during** a fake POST with result committed, second worker refused, and a parked Run retaining admission after an operator GET. These are not live routing or billing tests.

| Changed files | Purpose |
|---|---|
| `deploy/oci/compose.crosscloud-staging.yaml`, `config.py` | Staging services and ingress-only file-backed OTLP credentials. Existing Compose files and Dockerfile unchanged. |
| `crosscloud/worker.py`, `crosscloud/dispatch.py`, `crosscloud/reconcile.py` | Graceful single worker per DB, shared reauthorization check, read-only operator evidence. Existing adapters, durable send rights, schema and registry reused. |
| `dashboard/app.py`, `dashboard/azure_run.py`, `dashboard/api_client.py` | Separate private deterministic Start and stored-status fragment; no change to Kaggle or historical views. |
| `tests/smoke_crosscloud_compose.py`, `tests/test_remote_dispatch.py`, `tests/test_dashboard_login.py` | Runtime packaging, recovery, shutdown and dashboard coverage. |
| `docs/phase4.2/phase4_2B1_deployment_readiness_report.md` | Operator handoff. |

Dashboard AppTest verified disabled controls, a lost local receipt followed by an explicit same-key recovery, selection of the returned **local** ID, then only private stored GETs. The operation key is server session state, not persistent across a lost browser session: investigate the receipt before starting from another session. The panel shows `reserved` (local only), `dispatching` (send right claimed), `accepted` (remote running), `terminal` or `uncertain`; business outcome and stored binding telemetry are separate. Binding telemetry can remain `unknown` even when ingress has a complete trace. Cancel is unsupported. Only the operator's deployment ID, fixed workload and 120-second deadline are submitted. Streamlit receives the user's Auth0 access token, **never** the Azure bearer.

## Staging topology and configuration

The existing isolated production Compose has explicitly named networks/volume and loopback ports: changing the project name alone would **not** isolate that file. Use only [the staging file](../../deploy/oci/compose.crosscloud-staging.yaml), project `aob-crosscloud-staging`, on a separately approved host. All four services use the same preloaded, reviewed image, UID/GID 10001, no builds or pulls at startup. The dashboard and control share a private network and login/JWKS egress; ingress has only its edge network. The three loopback host ports default to 28501 (UI), 28010 (private API), 28011 (telemetry). The staging proxy must expose **only** the intended telemetry write routes; never proxy private Run routes publicly. Use the existing authenticated private access path for the dashboard. No staging Kaggle launch credentials are supplied.

Private control, ingress and dispatcher share project volume `data` at `/var/lib/aob`, demo DB `/var/lib/aob/demo.db`. Use a single host with a local filesystem supporting SQLite and `flock`, not multi-host/NFS or multiple volume clones. The control lifespan adds schema before its healthy response; the worker depends on that health check. The worker has **no listener or port binding**, only `dispatch-egress`, read-only root, dropped capabilities, 1 CPU/384 MiB/128 PIDs, restart `on-failure:3`, 100 s drain. Never scale it or run a second supervisor. A nonblocking per-DB lock is released by process exit; never delete the lock file. SQLite send rights still fence a crash. Compose's egress bridge **is not a firewall**: the network owner must enforce and verify outbound traffic from that bridge limited to the approved numeric IPv4 TCP/443, denying other external, host/metadata and lateral destinations (including IPv6); no proxy or DNS fallback. Verify routing and effective rules independently before enabling dispatch.

Example operator-managed config outside the checkout (`STAGE_ENV`, mode 0600; no bearer values):

```dotenv
AOB_STAGING_IMAGE=<preloaded-reviewed-arm64-image@sha256:digest>
AOB_STAGING_SECRETS_DIR=/secure/staging-only/aob-secrets
AOB_AUTH0_DOMAIN=<staging-tenant-host>
AOB_AUTH0_AUDIENCE=<staging-api-audience>
AOB_CROSS_CLOUD_ENABLED=false
AOB_REMOTE_RUNS_ENABLED=false
AOB_OTLP_READ_SOURCES=["<approved-source>"]
AOB_AZURE_RUN_DEPLOYMENT_ID=
AOB_AZURE_RUN_HOST=
AOB_AZURE_RUN_IPV4=
```

File mounts: provision `allowed_users.json` (enabled subject, exact `external_sources` and `cross_cloud_scopes`), `event-secret` (signed callback APIs only), `streamlit-secrets.toml` (staging Auth0 login, audience, exposed access token and required scopes), `azure-run-token` (worker only, corresponding to Azure `RUN_API_TOKEN`), `otlp-tokens.json` (ingress only, `{"<source>":"<independent-ingest-token>"}`, corresponding to Azure `OTLP_SOURCE_TOKEN`). The last two mount via `AOB_AZURE_RUN_TOKEN_FILE` and `AOB_OTLP_SOURCE_TOKENS_FILE`. Verify **distinct** secret values via the approved secret manager without displaying them; a worker deliberately cannot compare secrets mounted in another service. Use restricted parent directory and host ownership/modes readable by mapped UID/GID 10001 (e.g. root:10001 0440). File-backed Compose secrets are read-only bind mounts: verify actual readability/non-writability; after atomic replacement recreate the affected service to pick up the new inode. Never supply an env dump, plaintext CLI argument, registry token or production secret. The read catalog is **not** a credential.

## Azure inventory gate (read only; not executed)

Require written scope for `SUB`, `RG`, `APP`, approved source, hostname and maintenance window. Use a read-only identity. Don't issue data-plane requests to a cold app or start a replica. Save only selected, redacted fields.

```bash
az account show --subscription "$SUB" --query '{subscription:id,tenant:tenantId,state:state}'
az group show --subscription "$SUB" -n "$RG" --query '{id:id,location:location}'
aca=(--subscription "$SUB" -g "$RG" -n "$APP")
az containerapp show "${aca[@]}" --query '{id:id,name:name,fqdn:properties.configuration.ingress.fqdn,traffic:properties.configuration.ingress.traffic,mode:properties.configuration.activeRevisionsMode,latestReady:properties.latestReadyRevisionName}'
az containerapp revision list "${aca[@]}" --all --query '[].{name:name,active:properties.active,health:properties.healthState,image:properties.template.containers[].image}'
# Resolve latestRevision entries to actual revisions. REV has 100% effective traffic;
# OLD comes from a signed prior successful release, not "the previous list item".
for r in "$REV" "$OLD"; do
  az containerapp revision show "${aca[@]}" --revision "$r" --query '{name:name,health:properties.healthState,images:properties.template.containers[].image,scale:properties.template.scale.{min:minReplicas,max:maxReplicas}}'
done
az containerapp revision show "${aca[@]}" --revision "$REV" \
  --query "properties.template.containers[].env[?name=='RUN_API_TOKEN' || name=='OTLP_SOURCE_TOKEN'].{name:name,ref:secretRef}"
az containerapp replica list "${aca[@]}" --revision "$REV" --query '[].{replica:name,state:properties.runningState,containers:properties.containers[].{name:name,ready:ready,restarts:restartCount}}'
```

Compare app ID and FQDN to inventory. Verify both digests against deployment-time records/build provenance (e.g. `gh attestation verify "oci://$IMAGE_AT_DIGEST" --repo "$APPROVED_SOURCE_REPO"` when that publisher supplies attestations); match signer, build workflow and exact source tree to review. A mutable tag looked up today, a dirty checkout or image label alone is insufficient. Verify known-good `OLD` digest, contract and rollback authorization; **do not change traffic**. Confirm secret *references*, one healthy traffic-bearing revision and one stable replica/process throughout the window, no rolling update, no autoscaling to another process. Pinning an ingress IP does **not** pin a revision/replica. Split traffic, stickiness assumptions or unverifiable source/digest block this test.

Verify the reviewed digest's `app/main.py`, `models.py`, `benchmark.py` and telemetry configuration: bearer on POST and both GETs, `external_run_id=azure-py-<UUID>`, exactly 100 tasks, concurrency 4, `workflow=synthetic-intake-v1`, `dataset_version=synthetic-v1`, deterministic/no inference, OTLP to the approved staging receiver with finalization enabled. POST may complete synchronously. Read-only contract verification needs no trial POST.

From approved inventory/resolver (operator only), verify the exact bare `HOST`, the DNS A/CNAME chain and TTL with `dig +noall +answer "$HOST" A`; match `IP` to both Azure ingress inventory and a permitted **public unicast IPv4**. Reject private, reserved, metadata or ambiguous destinations. With separate authorization for the already-running endpoint:

```bash
openssl s_client -connect "$IP:443" -servername "$HOST" -verify_hostname "$HOST" \
  -verify_return_error -CApath /etc/ssl/certs </dev/null
pin=(--noproxy '*' --proto '=https' --resolve "$HOST:443:$IP" --max-time 10 -sS)
# KNOWN_REMOTE is a verified existing azure-py-UUID. RUN_HEADER is a protected
# file with the Authorization: Bearer ... header from the Run credential only.
url="https://$HOST/demo/runs/$KNOWN_REMOTE"
curl -q "${pin[@]}" -o /dev/null -w '%{http_code}\n' "$url"
for path in "$url" "$url/units"; do
  curl -q "${pin[@]}" -H "@$RUN_HEADER" -o /dev/null -w '%{http_code}\n' "$path"
done
```

Expect 401 then 200/200. Stop on redirect, TLS error, changed pin or unexpected status. Runtime HTTP connects to the numeric pin, uses the verified hostname for Host/SNI/certificate verification and does not resolve user destinations, follow redirects or use ambient proxy settings. ACA shared ingress and DNS rotation are compatible **only while the verified pair remains valid**. Freeze admissions on drift, investigate outstanding bindings against their original snapshot; never silently repin an in-flight Run. Approve a new configuration/window separately.

## OCI staging, ARM64 and rollback gate (not executed on OCI)

1. On a separately authorized **native** ARM64 test builder: `test "$(uname -m)" = aarch64`. Export the tested Git tree into a new disposable build context (`git archive bd0ede5 | tar -x -C "$EMPTY_CONTEXT"`). Use `docker buildx build --platform linux/arm64 --load -t "$UNUSED_TEST_TAG" "$EMPTY_CONTEXT"`; record build provenance/base digest and resulting platform/image ID. `docker image inspect "$UNUSED_TEST_TAG" --format '{{.Os}}/{{.Architecture}} {{.Id}}'`; `docker run --rm --pull=never --network none "$UNUSED_TEST_TAG" python -c 'import platform,os,crosscloud.worker,crosscloud.reconcile,api.private_app,api.public_ingress; assert platform.machine()=="aarch64" and os.getuid()==10001'`. Run the same `tests/smoke_crosscloud_compose.py` against this image on that disposable local daemon. Attest the digest and promote only that artifact via the approved release channel. Local host/buildx offered amd64 variants only: **native ARM64 remains a gate**.
2. Before staging deployment verify host/daemon, project, volume, free resources, original image and proxy config; take the backup below. Fail if project names, ports, mounts or storage overlap production. Use a sanitized operator shell (no inherited flag/target overrides). Bash shorthand:

   ```bash
   set -euo pipefail; set +x; umask 077
   dc=(docker compose --env-file "$STAGE_ENV" -p aob-crosscloud-staging -f deploy/oci/compose.crosscloud-staging.yaml)
   "${dc[@]}" --profile dispatch config --quiet
   "${dc[@]}" up --wait --no-build control-api event-ingress dashboard
   "${dc[@]}" ps
   ```

3. Default flags off: `/api/v1/runs/nonexistent` returns 404 on both APIs. For registry-only staging, set `AOB_CROSS_CLOUD_ENABLED=true`, leave remote false, recreate control/dashboard using the `up` command. Check healthy, private route 401 without auth, ingress and public telemetry origin still 404; confirm actual proxy mappings. With an authorized owner's bearer and `aob:deployments:write`, register or verify an existing owner-scoped Azure **agent**, `execution_policy=execute`, `connection_mode=public_https`, capabilities `["start","status"]`, explicit source, `endpoint_ref=ref:azure-agent`, `credential_ref=ref:azure-run` using `POST /api/v1/deployments` with a stable registration `Idempotency-Key`. Registration is not approval. Verify via private GET, record immutable ID/version. Configure that ID, verified host and IP. Never configure a Kaggle dispatch ID here.
4. Only after authorization, with no pending dispatches/legacy runs, one authorized owner, access restricted to the test operator and the egress policy active, set `AOB_REMOTE_RUNS_ENABLED=true` and run `"${dc[@]}" --profile dispatch up --wait --no-build`. Verify one worker, correct volumes/network/secrets, resource limits and no worker port mapping with `docker inspect` projections as in the [G2 inventory runbook](phase4_2B_implementation_report.md). `up` can consume pending work immediately. Do **not** use it to inspect a copied queue.
5. To close the window: prevent new admissions first (restrict callers, flag off and recreate API/dashboard), `"${dc[@]}" stop run-dispatcher`, check exit status and last stored state. SIGTERM lets a bounded current operation finish; a hard kill leaves recovery to the durable lease. Disabling only one process is not a stop switch for the other. Keep registry read access if needed. No cancellation, deletion or provider cleanup implied.

**Backup and rollback.** Fence Start and quiesce all writers incl. ingress, capture any outstanding Run mapping and provider evidence. Record current Compose revision, image IDs, UID/GID, actual volume/path mappings, secret/config versions and routing. Stop the staging project services; do not remove its volume. With the verified data volume mounted **read-only** on a trusted offline helper, use SQLite `.backup` for each existing `demo.db` and `intake.db` into a new restricted encrypted backup directory. For example `sqlite3 -readonly /snapshot/demo.db ".backup '/backup/demo.db'"`, then `sqlite3 -readonly /backup/demo.db 'PRAGMA integrity_check;'`; repeat for intake if present, record absence if not used. Include matching raw/attachment/result files, WAL context for the reader, and any separate mounts in the same quiescent window. Record checksums; rehearse restore/old and new readers on disposable copies. Never copy a live main DB alone.

Prefer flag-off, prior reviewed image/config and **retaining the additive DB**. If restore is actually authorized, keep an additional post-incident snapshot, restore validated backups/associated files onto a **fresh separate** volume with correct ownership and no stale WAL/SHM. Start readers only with no dispatcher, isolate Start/legacy routes, verify integrity, owner/source denials and matching histories. Reconcile all sends since backup before any admission. Restoring a pre-send snapshot, starting an old image unaware of dispatches, or using a blank DB to get a free slot could duplicate a real execution. An unresolved identity blocks resumption, regardless of rollback.

## Uncertain Run procedure and release blocker

Record in a restricted incident ticket: named investigator, authorizer, UTC window, reason, local ID, owner/source, deployment/version, preallocated remote ID, flags, snapshot host/IP, current state/error, send attempts, deadline, Azure revision/digest, replica/restart timeline and redacted ingress/trace references. Do not log tokens, request bodies or DB dumps.

| Observation | Supported action |
|---|---|
| POST reply lost or worker died after claim | Stop other starts. Before deadline the dispatcher may reconcile the same ID by GET (after a crash the claim can last 90 s). Never replay POST. `attempts=1` is one **send right**, not a wire count. |
| Known matching terminal result during normal reconciliation | Worker records terminal business state; verify telemetry separately. No operator DB action. |
| Azure restarted, GET 404, timeout, invalid response or unknown ID | Process-local history can disappear. Confirm exact mapping, replica and revision; unknown is **not** evidence of non-execution. Do not search by guessing IDs or start a replacement. |
| Deadline elapsed before any send | Existing worker may record terminal pre-send denial. Distinguish from a crash after a claim or possible delivery. |
| Expired after possible send | Worker parks uncertain; global slot remains held, including against legacy admissions. Expiry is neither failure nor cancellation at Azure. |

**Status-only observation, including after deadline:** with incident authorization on the approved host, existing flags/config, non-cold target and an uncertain local ID, run:

```bash
"${dc[@]}" exec -T run-dispatcher python -m crosscloud.reconcile "$LOCAL"
# If the worker was stopped, use the same approved target and enabled flags in a
# one-shot process instead (no dependencies, no worker loop):
# "${dc[@]}" run --rm --no-deps -T run-dispatcher python -m crosscloud.reconcile "$LOCAL"
```

Record the command, exit status, UTC and returned sanitized observation in the ticket. This is a privileged OS/operator operation, not a dashboard endpoint. It reads stored identity/snapshot in SQLite read-only mode, rechecks owner grant/source/deployment and original pin, does **at most one authenticated GET** with a fresh <=10 s read budget, and **does not write or release the slot**. A zero exit means an observation was produced, not that the result is conclusive. A revoked grant/changed target blocks it. Do not turn on the API or launch the worker merely to investigate.

**Blocker:** there is no supported way to safely release a permanently parked slot under the current process-local remote contract. Partial/complete OTLP, absence of logs, expiry or 404 alone cannot adjudicate it. Leave it held. For a separately designed change require durable authenticated provider evidence tied to exact source, ID, immutable target, outcome and absence of outstanding work; corroborate with complete ingress/access records, incident backup and independent provider owner + control owner approval. Propose an append-only signed adjudication record, compare-and-set on the still-uncertain binding, atomic audit/slot transition, no send capability and explicit handling of insufficient evidence. No such administrative operation is shipped. Without proof the decision is **remain blocked**, not force-unlock, edit SQL, wipe storage, retry or delete.

## Exactly one controlled real Run (future authorization only)

1. Obtain named approvers for OCI and Azure resources, cost/window and evidence capture; all gates above, including the response to a permanently held slot, must be accepted. Freeze revision/replica/traffic; identify authoritative unsampled request logs for the sole traffic-bearing revision/replica covering the entire test window and any proxy retries. Restrict other starts. If there is no trustworthy POST count, do not claim acceptance.
2. Use **one** submission path, not both UI and CLI. For a reproducible CLI run, set `BASE=http://127.0.0.1:28010`, `DEPLOY` and `SOURCE` from approved inventory, `AUTH_HEADER` to a protected file holding the **user** authorization header (`aob:runs:execute` and `aob:runs:read` in JWT and allowlist, explicit source). Choose a fresh private evidence directory `E`; record a fresh UUID operation key and the UTC start once. No bearer on the command line. In a restricted shell:

   ```bash
   U=$(python -c 'import uuid; print(uuid.uuid4())')
   printf '%s\n' "$U" > "$E/operation-key"
   jq -n --arg id "$DEPLOY" '{agent_deployment_id:$id,workload_version:"synthetic-intake-v1",deadline_seconds:120}' > "$E/spec"
   private=(--noproxy '*' --max-time 10 -sS -H "@$AUTH_HEADER")
   code=$(curl -q "${private[@]}" -o "$E/receipt" -w '%{http_code}' \
     -H "Idempotency-Key: $U" -H 'Content-Type: application/json' --data-binary "@$E/spec" "$BASE/api/v1/runs")
   test "$code" = 202
   LOCAL=$(jq -er '.run_id|select(test("^[0-9a-f]{32}$"))' "$E/receipt")
   ```

   No curl retries or redirects. If receipt is lost, stop; authorized recovery of the **local receipt** must use exactly the saved key/body/owner, never POST to Azure or generate a new key. In UI use **Start Azure Run** once; the receipt selects the local Run and subsequent fragment polls are GET only.
3. Poll `curl -q "${private[@]}" -f "$BASE/api/v1/runs/$LOCAL" > "$E/status"` at five-second intervals, bounded by the 120 s deadline plus a short evidence window. Record transitions, `source_id`, `agent_deployment_id`, version, `remote_run_id`, attempts and business outcome. Require `terminal`, `complete`, attempts 1 and exact source/deployment; any ambiguous result follows the incident procedure, not another Start.
4. Set `R` to the verified stored `azure-py-<UUID>` and use `pin` and `RUN_HEADER` from the inventory gate (restricted operator context). Verify both provider GETs and stored telemetry:

   ```bash
   curl -q "${pin[@]}" -f -H "@$RUN_HEADER" "https://$HOST/demo/runs/$R" > "$E/remote"
   curl -q "${pin[@]}" -f -H "@$RUN_HEADER" "https://$HOST/demo/runs/$R/units" > "$E/units"
   jq -e --arg r "$R" '.run_id==$r and .status=="COMPLETE" and .completed==100 and .failed==0 and .total==100' "$E/remote"
   jq -e 'length==100 and (map(.unit_id)|unique|length)==100 and all(.[];.status=="COMPLETE") and (group_by(.worker_id)|map(length))==[25,25,25,25]' "$E/units"
   path="$BASE/api/external/runs/$R"
   curl -q "${private[@]}" -fG --data-urlencode "source_id=$SOURCE" "$path/summary" > "$E/summary"
   jq -e --arg r "$R" --arg s "$SOURCE" '.run_id==$r and .source_id==$s and .trace_status=="TRACE_COMPLETE" and .unique_units==100 and .completed_units==100 and .failed_units==0 and .worker_count==4 and .validation_passed==100 and .trace_count==1 and .span_count==405 and .valid_parent_links==404 and .external_parent_links==1 and .unresolved_parent_links==0 and .llm_calls==0' "$E/summary"
   curl -q "${private[@]}" -fG --data-urlencode "source_id=$SOURCE" "$path/traces" > "$E/traces"
   jq -e --arg trace "$(jq -er .trace_id "$E/remote")" '.items|length==1 and .[0].trace_id==$trace and .[0].span_count==405' "$E/traces"
   ```

   Allow a bounded finalization wait for stored telemetry, never another dispatch. Match business and trace IDs through the stored binding. Confirm recorded deterministic source/config and no inference integration; zero observed LLM calls alone is insufficient on an incomplete trace.
5. Across the frozen window count **exactly one** ingress `POST /demo/runs` (including retries/errors, all replicas/revisions) attributable to this isolated execution; correlate request time, mapping and trace. Preserve redacted log query, coverage and count, not secret headers or bodies. Compare the local receipt/attempts and terminal history. If logs are sampled, missing, or show duplicates, mark inconclusive/failed even if totals and trace match. Verify replica/revision unchanged after the test. Close admission, stop dispatcher, preserve backups and evidence, obtain independent review.

**Stop here.** Native ARM64 packaging, live inventory/TLS/egress, provenance/rollback, credentials, proxy isolation, real one-POST evidence and the authorized real Run remain unverified. No live cross-cloud acceptance, RunPod, connector or MCP release is implied.
