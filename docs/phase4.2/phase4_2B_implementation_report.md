# Phase 4.2B: remote Agent execution, local G2

**Decision: G2 passed locally. No live execution or release approval.** The private API can reserve a Run without contacting a provider. The control worker may start it once; after losing a response or its process, it queries the same remote ID. This is not exactly-once remote execution.

## Baseline and inventory

`feature/phase4-2-crosscloud` started clean, tracking its matching origin branch, at `70c29213ed80be3e3697419f1fe65663da22dd67`. Registry, policy, reader, migration and verification changes were committed; no interrupted edit or concrete G1 implementation failure was found. Final tested implementation and tests: `c9b995b6e419e28545b404b0b0a9686ce0f944eb`. The final commit adds only this report; obtain its exact hash with `git log -1 --diff-filter=A --format=%H -- docs/phase4.2/phase4_2B_implementation_report.md` (a commit cannot contain its own hash).

The sibling checkout was `f1ca67970cb2e8b8120f154d031f8f51f456d5e0` with eight pre-existing modified sample files. We inspected and tested that working tree, not an assumed deployed revision. No sibling files were changed by this implementation.

| Group | Changed files |
|---|---|
| Kaggle (`627341d`, deadline integration in `648eaa6`) | `crosscloud/kaggle.py`, `demo_service.py`, `demo_store.py`, `kaggle_launcher.py` |
| Azure and operator resolution (`82c91a0`, `ac47535`, hardening `ef8f19f`) | `crosscloud/azure.py`, `crosscloud/resolution.py`, `crosscloud/providers.py` |
| Durable command and control (`648eaa6`, `ef8f19f`) | `config.py`, `crosscloud/commands.py`, `crosscloud/store.py`, `crosscloud/dispatch.py`, `crosscloud/worker.py` |
| Private transport and scoped projection (`f952a90`) | `api/deployments.py`, `crosscloud/queries.py` |
| Verification (`c9b995b`) and handoff | `tests/test_remote_dispatch.py`, this report |

**Actual G1 coverage:** `tests/test_crosscloud.py` contains three compound tests. They exercise verified-token errors, missing scope, owner/source filtering, revocation, flag and ingress isolation; 16 same-key registrations on independent connections, one receipt, payload conflict, separate owner namespace and fresh-interpreter replay; and scoped bindings, read-only projections and lack of dispatch. The fixture initializes schema on an existing legacy row. It does **not** by itself rehearse an upgrade/restore of a populated database, a concurrent Run dispatch or a lost remote response. The migration rehearsal and new G2 tests are separate evidence.

## Contracts and safeguards

* **POST `/api/v1/runs`** on the private app: verified Auth0 access token, enabled user, `aob:runs:execute` in both token and operator grants, exact owner and explicitly granted non-null source. Requires `Idempotency-Key` (1-128 characters). Example: `{"agent_deployment_id":"<registered-id>","workload_version":"synthetic-intake-v1","deadline_seconds":120}`. Optional W3C version-00 `traceparent`; no baggage, arbitrary headers or user URLs. Returns **202** `{"run_id":"<stable-local-id>"}` on reservation and identical authorized replay; changed payload with the same key or a competing Run gets 409. This receipt is not a provider acknowledgement.
* **GET `/api/v1/runs/{run_id}`** requires `aob:runs:read` in both grants. Returns stored, scoped history (including business/telemetry status, mapping and dispatch state for bound Runs); inaccessible and absent IDs both give 404. The new reader does not poll, lease work, test reachability or invoke inference. No new public ingress route. Existing Dashboard/Kaggle endpoints retain their previous behavior, including legacy active-run polling.
* **Two flags, both default off:** `AOB_CROSS_CLOUD_ENABLED` mounts the private registry and migrates; `AOB_REMOTE_RUNS_ENABLED` independently permits private execution. Registry enablement alone never starts a worker. For a separately approved installation, start the private API first, then supervise `python -m crosscloud.worker` as a separate process with the same DB, flags, allowlist, source catalog and operator configuration. The command is **not** installed as a Compose service by this change. Stop that process to halt new sends; disabling the API flag alone in a different process does not recall accepted work.
* The operator approves the immutable **deployment ID**, not a caller's choice of host. Deployments must be registered `agent`, `execute`, `public_https` with `start` and `status`. The worker rechecks operator grants, source, registration and target against the snapshot before contacting a provider. No private connector, generic adapter, provision, delete, inference or remote cancel path. Both built-ins report remote cancellation as `NOT_SUPPORTED`.
* **Azure:** `AOB_AZURE_RUN_DEPLOYMENT_ID`, aliases `ref:azure-agent` / `ref:azure-run`, `AOB_AZURE_RUN_HOST` (bare TLS hostname), `AOB_AZURE_RUN_IPV4` (approved numeric public destination), `AOB_AZURE_RUN_TOKEN_FILE` (separate restricted bearer file). Pin both address and hostname; no destination DNS lookup, ambient HTTP proxy, redirects, custom port/path or TLS bypass. The TLS SNI/certificate name and Host header remain the approved hostname. Rotate the file without changing deployment records. Never reuse OTLP tokens; the worker rejects equality with locally configured ingestion tokens, but split services must also provision independent secrets. A pin change requires operator review; in-flight work is not redirected to a different target.
* Azure `POST /demo/runs` sends a preallocated `azure-py-<UUID>`, 100 tasks, concurrency four, fixed sample workflow/dataset defaults and the Run bearer. POST can **finish synchronously**; 202/RUNNING is not business completion. Map terminal status and counts, not HTTP success alone. Connection budget <=5 s, total POST <=75 s, GET <=10 s, also capped by remaining deadline; Azure admission allows 1-120 s. Cap response bytes, discard provider text and expose only local error codes. Query status via `GET /demo/runs/{remote_id}`; optional units projection via `GET /demo/runs/{remote_id}/units`. No inference settings change. Business success does not mark the binding's telemetry complete; consult stored ingress evidence.
* **Kaggle:** `AOB_KAGGLE_RUN_DEPLOYMENT_ID`, aliases `ref:kaggle-agent` / `ref:kaggle-run`, existing private notebook/CLI credentials and event/trace configuration. Only `kaggle-light-v1` (10 units) on the new route, deadline 1 through the lesser of 10,800 s and the configured demo limit. Reserve the actual legacy `run-<hex>` callback row under the shared slot, cooldown and daily cap. Reuse submit, status grace, timeouts, signed event/trace callbacks and validation result. The remote identifier is the notebook correlation ID, **not** a per-version Kaggle status endpoint. A kernel terminal signal alone cannot prove validation passed. Legacy routes, modes, workloads, Dashboard, GPU and scaling logic were not replaced.

## Dispatch and recovery

One `BEGIN IMMEDIATE` transaction commits the operation receipt, local/remote binding, workload/deadline, deployment version and snapshot, approved target, trace context and `reserved` dispatch. A companion `dispatch_context` table is additive to the G1 tables. Observed bindings have no dispatch and are never queued. Global admission checks both legacy active rows and nonterminal dispatches in the same writer transaction.

| State / observation | Control action |
|---|---|
| `reserved` | Claim a unique send right, increment attempts, commit `dispatching` and a 90-second claim before any remote I/O. |
| Valid RUNNING / terminal response | Store `accepted` / `terminal` and business outcome. Later control polls update accepted Runs. |
| Explicit remote rejection or pre-send authorization/deadline denial | Terminal local failure, safe code; no remote cleanup. |
| Timeout, lost response, ambiguous outage, malformed response or missing remote record | `uncertain`, business unknown. Poll the same remote ID, never POST again. A token fences stale worker results. |
| Crash during or before send after claim | Once the claim expires, status-only recovery. It may sacrifice a Run that was never actually transmitted. |
| Deadline passes after possible send | Park `uncertain`, retain slot; do not declare cancellation or failure at provider. |

Eligible polls are scheduled at five-second intervals. `attempts` counts reserved send rights, not proof of packets or billing. Azure's replay history is process-local; unknown after restart can remain unknowable. A parked uncertain Run requires out-of-band operator investigation and blocks new admission. There is intentionally no force-unlock, automated retry or resource deletion. Do not clear the row to bypass the slot. Neither worker supervision nor a manual adjudication workflow was deployed or tested on live infrastructure.

## G2 evidence

Separate CPython 3.12 environments installed `requirements.lock` and the sibling sample's hash-checked `requirements-test.lock`; both passed `pip check`. AOB used isolated HOME/runtime, empty inherited environment, `AOB_AZURE_TEST_PYTHON` pointing to the sibling interpreter, and `python -m pytest -q -ra -p no:cacheprovider --basetemp=<fresh-path>`. All provider execution was mocked or on local loopback.

| Executed | Passed | Failed / skipped |
|---|---:|---:|
| Starting AOB / Azure sample | 150 / 10 | 0 / 0 |
| Final AOB / unchanged Azure sample | **161 / 10** | **0 / 0** |
| Old signed-trace fixture generation / G1 fixture generation (outside suite counts) | 1 / 1 | 0 / 0 |

One existing Starlette TestClient deprecation warning remains in AOB.

- [x] Legacy Kaggle API, callbacks and traces green; new adapter shares admission and does not invent a passing result.
- [x] Actual sample on loopback: synchronous POST returned 200, test transport discarded it after execution; worker recorded uncertain, then made **one GET, no second POST**. Same remote ID, 100 distinct units, four workers, complete 405-span trace, propagated parent, zero LLM calls.
- [x] Concurrent same-key starts returned one stable ID. A separate worker process exited at the send boundary; competing recovery workers made one status call. Unknown remote state did not cause replay. Expiry before send and after possible acceptance are distinct.
- [x] Scope, owner, source revocation between admission and dispatch, default-off, invalid workload/deadline, unauthorized target, private ingress isolation, stored-only reads and occupied slot verified.
- [x] Rejected private/loopback/metadata destinations, unsafe redirect not followed, bearer file rotation, no provider body or fake bearer in returned error, captured logs or SQLite dump. Real TLS/egress remains a live gate.
- [x] Pre-upgrade fixture from `c88fe266`: `tests/rehearse_phase4_migration.py` passed migration, restart, original row projections, old reader, restored backup and integrity checks. Separately backed up the populated G1 fixture produced by `test_scoped_bindings_and_side_effect_free_history` at `70c2921`; applied G2 schema twice. All 12 previous tables retained identical ordered rows, including 3 deployments, 2 bindings and 5 receipts; zero dispatch contexts, integrity `ok`.

## Operator runbook: read-only live gates (not executed)

Use only a pre-existing, approved staging resource and read-only identity. Record selected fields, timestamp and operator, not raw CLI output, headers, environment dumps, secrets, DB contents or tokens. **Missing staging, unavailable digest or unknown rollback target is a release gate, not permission to create or wake a resource.**

### Azure resource, routing, digest and rollback

Set `SUB`, `RG`, `APP` to independently approved inventory identifiers. These commands do not change subscription defaults, revisions or traffic:

```sh
az containerapp show --subscription "$SUB" -g "$RG" -n "$APP" \
  --query '{id:id,mode:properties.configuration.activeRevisionsMode,host:properties.configuration.ingress.fqdn,traffic:properties.configuration.ingress.traffic,latestReady:properties.latestReadyRevisionName}' -o json
az containerapp revision list --subscription "$SUB" -g "$RG" -n "$APP" --all \
  --query '[].{rev:name,active:properties.active,health:properties.healthState,created:properties.createdTime,images:properties.template.containers[].image}' -o table
# REV is a traffic-bearing revision; OLD is a separately identified known-good rollback revision.
for r in "$REV" "$OLD"; do
  az containerapp revision show --subscription "$SUB" -g "$RG" -n "$APP" --revision "$r" \
    --query '{revision:name,health:properties.healthState,containers:properties.template.containers[].{name:name,image:image,envNames:env[].name}}' -o json
done
az containerapp replica list --subscription "$SUB" -g "$RG" -n "$APP" --revision "$REV" \
  --query '[].{name:name,running:properties.runningState}' -o table
az containerapp revision show --subscription "$SUB" -g "$RG" -n "$APP" --revision "$REV" \
  --query "properties.template.containers[].env[?name=='RUN_API_TOKEN'].secretRef" -o json
```

Match the traffic revision(s), container image `@sha256:...` and build provenance to the exact reviewed source. Record OLD's revision, digest and known-good contract before approving rollback. A mutable tag's **current** registry digest is not proof of what is running. If only a tag is available, require attested deployment-time digest evidence for both images or stop. Verify `RUN_API_TOKEN` has a secret binding by projecting only `env[?name=='RUN_API_TOKEN'].secretRef`, never `.value` or a secret-list command.

Do not invoke a cold/scale-to-zero app (even GET can wake it). With operator approval for an **already running** target, set `HOST` to the verified ingress hostname, `R` to an existing non-sensitive sample Run ID, and `RUN_TOKEN_FILE` to the trusted Run bearer file. Disable shell tracing. Do not POST, follow redirects or print response bodies:

```sh
set +x
base="https://$HOST/demo/runs/$R"
curl --noproxy '*' --proto '=https' --max-time 5 -sS -o /dev/null -w '%{http_code}\n' "$base"
for path in "$base" "$base/units"; do
  { printf 'Authorization: Bearer '; tr -d '\r\n' < "$RUN_TOKEN_FILE"; printf '\n'; } |
    curl --noproxy '*' --proto '=https' --max-time 8 -sS -H @- -o /dev/null -w '%{http_code}\n' "$path"
done
```

Expect 401 without auth, 200 on known history with Run bearer. A 404 may be lost process history, not proof a Run never existed. Read-only confirmation of the **POST** contract requires matching the deployed digest to source/attestation: check `app/main.py` for bearer guard and `await execute(...)`, `app/models.py` for strict request fields/defaults, `app/benchmark.py` for four workers and timeout. No live POST to prove it. Confirm ingress resolves to approved public IPv4, TLS certificate matches hostname, credential separation and no inference enabled. Unknown contract/replica affinity or conflicting traffic revisions block release.

### OCI staging routes, mounts, ARM64 and backup

On the existing staging host, select the actual Compose project from inventory. No `up`, `pull`, build, shell environment dump, proxy edit or secret inspection:

```sh
docker compose ls
docker ps --filter "label=com.docker.compose.project=$PROJECT" \
  --format '{{.ID}} {{.Label "com.docker.compose.service"}} {{.Image}} {{.Ports}}'
# Set CTRL, ING and DASH to the verified staging container IDs, not production names.
for c in "$CTRL" "$ING" "$DASH"; do
  docker inspect --format '{{.Name}} imageID={{.Image}} user={{.Config.User}} networks={{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}} ports={{json .NetworkSettings.Ports}}' "$c"
  docker inspect --format '{{range .Mounts}}{{println .Type .Name .Destination .RW}}{{end}}' "$c"
  docker image inspect "$(docker inspect --format '{{.Image}}' "$c")" \
    --format '{{.Id}} {{.Os}}/{{.Architecture}} {{json .RepoDigests}}'
done
uname -m
docker exec "$CTRL" python -c 'import platform,os; print(platform.machine(),*[os.getenv(k) for k in ("AOB_ENV","AOB_DB_PATH","AOB_DEMO_DB_PATH","AOB_CROSS_CLOUD_ENABLED","AOB_REMOTE_RUNS_ENABLED")])'
docker exec "$CTRL" grep -nE 'include_router|app.mount|app.get' /app/api/private_app.py
docker exec "$ING" grep -nE 'include_router|app.mount|app.get' /app/api/public_ingress.py
```

Confirm `linux/arm64`, actual host `aarch64`, healthy running image, matching volume names/DB paths and isolation. Inspect existing proxy routing with **GET** to a nonexistent ID via operator-confirmed origins (no auth/redirects); on the checked-in isolated compose the loopback defaults are:

```sh
for origin in http://127.0.0.1:18010 http://127.0.0.1:18011 "$PUBLIC_ORIGIN"; do
  curl --noproxy '*' -sS --max-time 5 -o /dev/null -w '%{http_code}\n' "$origin/api/v1/runs/nonexistent"
done
```

Flag-off: 404. Mounted private route: unauthenticated 401 (or configuration failure to investigate). Ingress/public telemetry origin: never a private handler, even with flag enabled. Also verify the actual proxy's route allowlist against `/api/events`, `/api/traces`, `/v1/traces`, `/v1/traces/complete`; HTTP status alone does not prove routing. Record previous OCI image digest and Compose revision. Run no GPU or inference probes.

**Backup procedure:** inventory actual DB paths, volume names and any raw/attachment/result files outside them. Arrange an operator-approved quiescent window and protected encrypted backup destination. Below reads the running container's DBs in SQLite `mode=ro` and writes only **new local backup files**. It does not stop writers, copy live WAL files, restore or mutate staging. For large DBs use an approved disk-backed SQLite backup job rather than serializing in RAM.

```sh
# Bash on staging host. Set BACKUP to an existing restricted, encrypted directory.
set -euo pipefail
umask 077
out=$(mktemp -d "$BACKUP/aob-staging.XXXXXXXX")
for key in AOB_DEMO_DB_PATH AOB_DB_PATH; do
  docker exec "$CTRL" python -c 'import os,sqlite3,sys; from pathlib import Path
p=Path(os.environ[sys.argv[1]]).resolve(strict=True)
s=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True); d=sqlite3.connect(":memory:")
s.backup(d); assert d.execute("pragma integrity_check").fetchone()==("ok",); sys.stdout.buffer.write(d.serialize())' \
    "$key" > "$out/$key.db"
  sqlite3 -readonly "$out/$key.db" 'PRAGMA integrity_check;'
done
(cd "$out" && sha256sum -- *.db)
```

Require `ok` for both copies and test old/new readers on a **disposable restored copy**, never the mounted DB. Back up corresponding files and approved config/secret **versions** through the existing protected backup system in the same window. Document restoration and previous-image inputs; do not assume two independently timed online backups form a consistent multi-file snapshot. If volumes, protected storage, quiescence, ARM64 image, proxy isolation or rollback evidence cannot be verified, keep execution off.

**Stop at G2.** Live Azure and OCI checks above are outstanding. Before even proposing Phase 4.2C execution, resolve these gates, agree on uncertain-Run adjudication, then obtain separate provider/account, model provenance, price, budget and test-window approval. No RunPod resources, LLM inference, paid provider calls, cloud deployment, Hybrid Connector or MCP work was performed.
