# Phase 4.2B-UI: repair the existing OCI workspace

**Decision:** The reported list 403s do not require an image change. Fix the *issued access-token scope and the mounted user grants*, then sign in again. A missing source grant makes a list **empty**, not forbidden. There is a separate reason to promote a new image for the complete rollout: on the reported release, disabling cross-cloud dispatch does **not** disable the whitelist-only legacy Kaggle start API. The two overlays below disable it, including for the Azure operator. Do not mistake hiding a button or removing credentials for denying admission.

This is an operator procedure, not evidence that OCI or Azure has passed acceptance. Use the existing project, data, tenant and edge. No changes to the Azure agent, Cloudflare, Caddy, host port mappings or RunPod. Stop at Stage 1 if a real run (including telemetry, network and logs) cannot be confirmed to incur **no incremental charge**.

## 1. What the observed responses establish

[Token verification](../demo_auth.py) uses the tenant's HTTPS JWKS, RS256, issuer `https://<domain>/` (trailing slash), configured API audience (must be present in `aud`), `exp`, and `sub`, with 10 seconds of leeway. Normal JWT time checks apply, including `nbf` if present. No email, role, username or `permissions` claim is required. Use an **access** token, not an ID token, client credentials, run bearer or OTLP token. Missing domain/audience is a configuration/startup error (503 in the dependency); missing/invalid/expired/wrong-audience bearer is 401.

`GET /api/me` additionally requires an enabled record for that exact subject. For the **same token, file and instant**, its 200 rules out bad issuer/audience and a disabled user. It says nothing about workspace permissions. The [private transport](../api/deployments.py) reads the file again and [constructs the principal](../crosscloud/policy.py):

```
effective scopes  = split(access_token.scope on whitespace) INTERSECT user.cross_cloud_scopes
effective sources = configured read-source IDs INTERSECT user.external_sources
```

Both comparisons are exact and case-sensitive. Absent/wrong-type scope or grant array yields no effective scopes. An Auth0 `permissions` array, role name, `scp`, wildcard or list-valued `scope` cannot substitute. There is no JWT-to-database ownership mapping other than `sub`.

| Request / response | Decision |
|---|---|
| `/api/me` 403 `detail: Demo access denied`; workspace 403 `code: ACCESS_DENIED` | No matching object with boolean `enabled: true` (including missing, unreadable, malformed or incorrectly nested allowlist). A file change between calls matters. |
| `GET /api/v1/deployments[/{id}]` including `?workspace=true`, `POST .../{id}/validate` | Missing effective `aob:deployments:read` -> 403 `INSUFFICIENT_SCOPE`. `workspace` changes the projection only. |
| `GET /api/v1/runs` with any filters, `GET .../runs/{local}` | Missing `aob:runs:read` -> same 403. No launch, provider poll or span required. |
| `GET /api/v1/observations/count`; `GET .../runs/{local}/observations/{summary,units,workers,timeline}` | Count needs `aob:traces:read`; sections need **both** runs:read and traces:read (all short scope names here have the `aob:` prefix). |
| `GET .../runs/{local}/llm-metrics` | Both runs:read and metrics:read. Missing metrics permission is not evidence of missing telemetry. |
| Register via `POST /api/v1/deployments` | deployments:write. The API generates the ID and sets owner from the verified token, never from the body. No global administrator/owner override; registry UI is a reader. |
| `POST /api/v1/runs` | Missing runs:execute -> `INSUFFICIENT_SCOPE`; observe-only target -> 403 `OBSERVE_ONLY`; dispatch disabled on a supported eligible target -> 403 `EXECUTION_DISABLED`; ID or endpoint/credential alias not approved -> 403 `TARGET_NOT_APPROVED`. Feature mount off -> 404, not 403. Unsupported capability/mode/status -> 409; invalid host/pin -> 503. Worker rechecks current owner, local execute grant, source, config and snapshot before sending; `TARGET_CHANGED` is a worker denial, not a list failure. Remote bearer rejection is a dispatch result, not an Auth0 read failure. |

The most specific diagnosis consistent with the logs is **missing effective read scopes**; the logs alone cannot distinguish missing requested/issued scopes, stale token, missing local lists or a different request principal. Capture only status and `code` with the same user token, compare the selected mounted record, requested OAuth parameters and actual token as below. Do not paste JWTs into a decoder site or a ticket.

### What a successful read can see

* [Registry](../crosscloud/registry.py): reads filter `owner_id = sub AND (source_id IS NULL OR source_id IN effective_sources)`. An unowned or out-of-source detail is 404, not 403. No per-user list of deployment IDs exists. `AOB_AZURE_RUN_DEPLOYMENT_ID` approves **one execution target**, not read access or delegation.
* [History](../crosscloud/queries.py): the workspace lists authorized **bindings**, joined to the agent deployment on identity, same owner and compatible source (null deployment source or matching binding source), excluding Kaggle. Span rows are not a prerequisite. Unbound legacy runs and unbound OTLP spans are **not** converted into workspace history. Do not rewrite owners, fabricate bindings or expect to recover missing records by broadening scopes.
* Observations first authorize the **local** binding, then query `(source_id, remote_run_id)`; matching remote IDs alone never correlate sources. Count is source-wide over granted sources, not an owner-specific run count. An authorized binding with no spans returns an empty timeline / zero `span_count`, not 403. No source grant => no source-bound rows; null-source deployments can still be visible.
* Legacy `/api/external/*` is a different source-only reader (no workspace scope check). With **one** configured source and an **absent** `external_sources` field it grants that source to an enabled user; `[]` explicitly denies. Review **all** enabled records before changing the catalog, especially when going from zero/multiple sources to one. An explicitly forbidden source is 404. Never use this compatibility rule as the workspace fix.

## 2. Existing Auth0 application and local policy

1. In the existing tenant, Applications -> APIs -> **existing API**. Match its Identifier exactly to the control API's audience, not the dashboard URL or Streamlit client ID. Use RS256. Define the permissions in the table below under Permissions. `openid`, `profile` and `email` are OIDC scopes, not AOB API permissions.
2. Under the API settings enable **RBAC** to enforce per-user assignments at issuance. Assign the corresponding API permissions directly to the user or to a role assigned to that user. Use no execute/write grant for an observer. RBAC is not a JWT claim the API validates; it is needed for this Auth0 role-based issuance policy (otherwise requested scopes may not be constrained by roles). Ensure tenant Actions/consent policies do not discard or widen the requested scopes. **Add Permissions in the Access Token is optional** and turning it on alone will not help.
3. Keep the existing Regular Web Application, Authorization Code login, client secret, discovery URL and cookie secret. Allow `https://aob.dipperwoven.com/oauth2callback` as the exact callback; retain approved logout redirects. Use the existing provider name (`AOB_AUTH_PROVIDER`, default `auth0`). No new application, machine-to-machine grant, Management API permission, refresh token or public API route is needed.

| Identity / operation | Minimum effective permissions |
|---|---|
| Registry and run lists | `aob:deployments:read aob:runs:read` |
| Stage 1 observer including historical agent telemetry | Above + `aob:traces:read`; add `aob:metrics:read` for a fully readable LLM metrics page |
| Execution-enabled operator using the full workspace | Full observer + `aob:runs:execute` (POST alone checks execute, not write). Must own the registered approved target and have its source. |
| Registry administrator for own resources | `aob:deployments:read aob:deployments:write` for list/get/validate/register; add observer permissions separately for history. No execute implied. |

`aob:runs:write` is used by internal binding/status commands, not by the workspace start or reads. Don't grant it for this release.

4. Edit the **mounted** `streamlit-secrets.toml`, retaining unrelated entries. For a full read workspace the relevant structure is:

```toml
[auth]
redirect_uri = "https://aob.dipperwoven.com/oauth2callback"
cookie_secret = "<retain existing value>"
expose_tokens = ["access"]
[auth.auth0]
client_id = "<existing application>"
client_secret = "<retain existing value>"
server_metadata_url = "https://<existing tenant hostname>/.well-known/openid-configuration"
[auth.auth0.client_kwargs]
scope = "openid profile email aob:deployments:read aob:runs:read aob:traces:read aob:metrics:read"
[auth.auth0.authorize_params]
audience = "<exact existing API Identifier>"
```

`openid` is required for this login; `profile email` are conventional for display, not authorization. `st.login(PROVIDER)` does **not** add any AOB scopes itself. Without an override the packaged Streamlit requests `openid email profile`; the prior sample requested only OIDC scopes. `scope` configures the client; `authorize_params` explicitly forwards `audience` on the authorization redirect. Inspect the `/authorize` request's **audience and scope only**, not cookies/callback codes. [The gate](../dashboard/auth_gate.py) reads `st.user.tokens["access"]`, calls `/api/me`, and [the client](../dashboard/api_client.py) sends that bearer to the private API. [The workspace](../dashboard/workspace.py) invokes separate registry, history, trace and metric reads.

For Stage 2 append `aob:runs:execute` to the application's requested scope string; RBAC and the allowlist must still limit it to the approved operator. Append deployments:write **only** for a client that must register resources. After changing Auth0 assignments, audience or requested scopes, recreate the dashboard if its config changed, **log out and sign in again**. Rerunning a Streamlit page does not change a signed token. Local file-only changes need no new token if the token already has the scopes; file secrets replaced by rename require **container recreation**, not just process restart.

### Exact allowlist shape

```json
{
  "allowed_users": {
    "<exact observer sub>": {
      "enabled": true,
      "external_sources": ["<approved stored source ID>"],
      "cross_cloud_scopes": ["aob:deployments:read", "aob:runs:read", "aob:traces:read", "aob:metrics:read"]
    },
    "<exact Azure owner sub>": {
      "enabled": true,
      "external_sources": ["<approved Azure source ID>"],
      "cross_cloud_scopes": ["aob:deployments:read", "aob:runs:read", "aob:traces:read", "aob:metrics:read", "aob:runs:execute"]
    }
  }
}
```

This is a field contract, not strict JSON Schema validation: the top-level map and subject record must be objects; literal boolean `true` enables; string arrays grant exact matches; unknown keys are not grants. Optional `username` is descriptive. Keep other users/metadata; never replace the entire file with this example. Omit execute in Stage 1. A distinct observer cannot see another owner's deployments merely by sharing a source. **There is no supported deployment-ID field in this JSON**; the corresponding placeholder belongs in `WORKSPACE_DEPLOYMENT=<approved existing deployment_id>` below.

## 3. Topology and preflight (OCI operator only)

The [standalone staging file](../deploy/oci/compose.crosscloud-staging.yaml) uses another project, project-local data, different loopback ports, staging event URL, separate networks and a profiled worker. It is **not** an override. The [isolated base in Git](../deploy/oci/compose.isolated.yaml) names the existing data volume, has three services, an env-based ingress credential and a dashboard secret targeted to `/run/secrets/streamlit_config`. The actual `compose.isolated.new.yaml` and `compose.phase4-2b-ui.yaml` are not in this checkout. Keep their working Streamlit secret location, database paths and auth/network wiring. Inspect actual merges; if the secret or volume **Compose key** differs from `allowed_users` or `aob-isolated-data`, adapt the reference in the Azure overlay to the **existing** key before proceeding. Never create a lookalike empty volume.

| Service | Preserve | Add/change |
|---|---|---|
| Dashboard | Production login, mounted active Streamlit secrets (standard `/app/.streamlit/secrets.toml` or verified custom secrets path), private `AOB_API_URL`, existing networks/port | `AOB_CROSS_CLOUD_ENABLED=true`, `AOB_REMOTE_RUNS_ENABLED=false` then true, approved deployment ID only in Stage 2. No DB, OTLP/run bearer or allowlist needed. |
| Control API | `AOB_AUTH0_DOMAIN` (bare hostname), `AOB_AUTH0_AUDIENCE`, allowlist and event secret paths, existing events URL, demo/intake DB paths, existing Kaggle mount/config | Both flags as dashboard; `AOB_LEGACY_DEMO_START_ENABLED=false` in **both** stages; `AOB_OTLP_READ_SOURCES` JSON array, never ingestion credentials. Stage 2 ID/host/IP for local admission/snapshot, **no Azure bearer**. No approved Kaggle ID. |
| Event Ingress | Existing Auth0/allowlist/event secret/events URL required by startup guard, existing DB and edge port; no `/api/v1` routes | Stage 1 leave working ingest unchanged. Stage 2 only `AOB_OTLP_SOURCE_TOKENS_FILE` -> read-only source-ID-to-token JSON; blank legacy inline variable. No run token, execute flags or read scopes. |
| Independent Run Dispatcher | None in Stage 1 | Stage 2 same two enabled flags, same read-source catalog/allowlist, demo DB, target ID/host/IP, `AOB_AZURE_RUN_TOKEN_FILE`; no other credentials. One `python -m crosscloud.worker`, no listener; read-only root, RW local volume (SQLite + per-DB `flock`), separate egress-only bridge, no edge/control network. |

Control and ingress must retain `AOB_ENV=production` with existing HTTPS `.../api/events`, or their **already validated** `staging` with exactly `http://event-ingress:8011/api/events`. Do not switch modes to make startup pass. Dashboard stays production. Cross-cloud startup applies additive, idempotent schema to **demo.db** before control becomes healthy; it does not import history into another database. Do not rebuild SQLite, backfill or edit rows.

Run these blocks in one private Bash maintenance shell with Docker, `jq`, `python3`, `curl` and authorized file access. Use root-equivalent access only on the intended OCI host. Use an encrypted, access-controlled backup location with sufficient space. Do not use shell tracing, `docker inspect` without projections, `compose config` to the terminal, `env`, or secret values on command lines. Do not copy tokens to the repository.

```bash
set -euo pipefail; set +x; umask 077
cd /srv/aob-isolated/releases/phase2
rel=$PWD; sec=/srv/aob-isolated/secrets; vol=aob-isolated-data
api=aob-isolated-control-api-1; ui=aob-isolated-dashboard-1; ing=aob-isolated-event-ingress-1
docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}} {{index .Config.Labels "com.docker.compose.project.environment_file"}}' "$api"
for c in "$ui" "$api" "$ing"; do
  test "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$c")" = aob-isolated
  docker inspect -f '{{.Name}} {{.Image}} {{json .HostConfig.PortBindings}} {{range .Mounts}}{{println .Source .Name .Destination .RW}}{{end}}' "$c"
  docker exec "$c" id
done
docker exec "$api" python -c 'import config as c; print(c.AOB_ENV,c.AUTH0_DOMAIN,c.AUTH0_AUDIENCE,c.ALLOWED_USERS_FILE,c.DEMO_DB_PATH,c.DB_PATH,c.get_otlp_read_sources())'
docker exec "$ing" python -c 'import config;print("ingest IDs only:",list(config.get_otlp_source_tokens()))'
docker volume inspect -f '{{.Name}} {{.Driver}}' "$vol"
docker ps --filter "volume=$vol" --format '{{.Names}}'
read -rp 'Absolute path to original Compose env file (empty if implicit .env): ' orig
envargs=(); if [[ -n "$orig" ]]; then test -f "$orig"; envargs=(--env-file "$orig"); fi
base=("${envargs[@]}" -p aob-isolated -f compose.isolated.new.yaml -f compose.phase4-2b-ui.yaml)
old() { docker compose "${base[@]}" "$@"; }
old config --quiet
for c in "$api" "$ing"; do
  test "$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/aob"}}{{.Name}}{{end}}{{end}}' "$c")" = "$vol"
done
sqlro() { docker exec "$api" python -c 'import sys;from crosscloud.store import reader
with reader() as c:
 for row in c.execute(sys.argv[1]): print(tuple(row))' "$1"; }
sqlro 'PRAGMA integrity_check'
sqlro 'SELECT deployment_id,provider,status,source_id,execution_policy FROM deployments'
sqlro 'SELECT source_id,count(*) FROM deployment_bindings GROUP BY source_id'
sqlro 'SELECT source_id,count(*) FROM external_agent_spans GROUP BY source_id'
sqlro 'SELECT state,count(*) FROM run_dispatch GROUP BY state'
sqlro 'SELECT status,count(*) FROM demo_runs GROUP BY status'
```

Check local-only bindings and actual DB on that volume, approved original env files/interpolation and exactly the expected writers. Stop on an unfamiliar service, active legacy run, nonterminal dispatch, missing table on this reportedly migrated deployment, failed integrity or missing expected history. Consult prior release/backup rather than guessing an ID. For **previously configured** pins use `docker exec "$api" python -c 'import os; print([os.getenv(k) for k in ("AOB_AZURE_RUN_DEPLOYMENT_ID","AOB_AZURE_RUN_HOST","AOB_AZURE_RUN_IPV4")])'`. Missing pins are **not derivable from a source name or registry ref**. Recover approved inventory from the release owner and verify independently.

### Backup and package gate

Fence all admissions and callbacks in the maintenance window; do not change public routes. A brief stop is needed for this whole-volume backup. Confirm no other writers (including non-container access). Capture old image tags separately so registry drift cannot change rollback. The backup contains secrets; restrict even after the window.

```bash
read -rp 'Existing protected encrypted backup parent directory: ' parent
test -d "$parent"; bk=$(mktemp -d "$parent/aob-phase42.XXXXXXXX")
cp -a "$sec" "$bk/secrets"
cp -a compose.isolated.new.yaml compose.phase4-2b-ui.yaml "$bk/"
if [[ -n "$orig" ]]; then cp -a "$orig" "$bk/original.env"; elif [[ -f .env ]]; then cp -a .env "$bk/original.env"; fi
printf 'services:\n' > "$bk/images.yaml"
for pair in "dashboard:$ui" "control-api:$api" "event-ingress:$ing"; do
  svc=${pair%%:*}; c=${pair#*:}; tag="aob-remediation-backup:${bk##*/}-$svc"
  # Docker tag components are case-sensitive; repository is lowercase.
  docker tag "$(docker inspect -f '{{.Image}}' "$c")" "$tag"
  printf '  %s:\n    image: %s\n' "$svc" "$tag" >> "$bk/images.yaml"
done
helper=$(docker inspect -f '{{.Image}}' "$api")
history_hash() { docker exec -i "$api" python - <<'PY'
import hashlib,json
from crosscloud.store import reader
with reader() as c:
    c.execute("BEGIN")
    for table in ("demo_runs","deployments","deployment_bindings","external_agent_spans","run_dispatch"):
        h=hashlib.sha256()
        for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid"):
            h.update(json.dumps(tuple(row),separators=(",",":")).encode()+b"\n")
        print(table,h.hexdigest())
PY
}
history_hash > "$bk/history.before"
old stop dashboard control-api event-ingress
test -z "$(docker ps -q --filter "volume=$vol")"
docker run --rm --pull=never --network none -u 0 -v "$vol:/snapshot:ro" "$helper" \
  tar --numeric-owner -C /snapshot -cpf - . > "$bk/volume.tar"
(cd "$bk"; sha256sum volume.tar > volume.sha256; sha256sum -c volume.sha256; tar -tf volume.tar | grep -Fx ./demo.db)
```

If either API stores data outside this volume, stop and back up those mounts before proceeding. Archive includes WAL/SHM, lock and companion files while quiescent. On failure keep fenced, use original services and intact volume; do not start an empty stack.

Use a separately reviewed ARM64 image containing the legacy gate. On a trusted native ARM64 builder with a clean reviewed checkout, e.g. `test "$(uname -m)" = aarch64 && docker build --platform linux/arm64 -t "agent-ops-board:oci-$(git rev-parse --short HEAD)" .`. Transfer using the approved offline release channel; retain distinct tags, verify provenance/digest on OCI. Provide the reviewed checkout with both overlay files on host.

```bash
read -rp 'Absolute reviewed checkout path: ' tree
read -rp 'Preloaded reviewed ARM64 image tag or digest: ' img
test "$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "$img")" = linux/arm64
docker run --rm --pull=never --network none -e AOB_LEGACY_DEMO_START_ENABLED=false "$img" python -c \
 'import os,config,api.private_app,api.public_ingress,crosscloud.worker; from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest,ExportTraceServiceResponse; assert os.getuid()==10001 and not config.LEGACY_DEMO_START_ENABLED'
for f in compose.workspace-read.yaml compose.workspace-azure.yaml; do
  test ! -e "$f"; install -m 0600 "$tree/deploy/oci/$f" "$f"
done
```

## 4. Stage 1: read-only workspace

Use the observed, explicitly approved catalog. Existing enabled records not intended for telemetry should have `"external_sources":[]`. Only grant readers access to sources they are authorized to see **in their entirety** on legacy source routes. In a secure editor merge the schema above and OIDC settings into copies; preserve other users and original secrets. These commands assume ordinary rootful Docker, runtime UID/GID 10001, no user namespace remapping; otherwise determine host mappings via `docker info --format '{{json .SecurityOptions}}'` and the host `/proc/<container-pid>/gid_map`. Use mapped IDs or a narrow file ACL. Never solve access by making secrets world-readable. Retain the restrictive original host directory permissions.

```bash
read -rp 'Approved read-source JSON array: ' sources
jq -e 'type=="array" and all(.[];type=="string" and test("^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$"))' <<<"$sources" >/dev/null
test ! -e workspace.env
printf "WORKSPACE_IMAGE=%s\nWORKSPACE_SOURCES='%s'\n" "$img" "$(jq -c . <<<"$sources")" > workspace.env
# Preserve implicit .env if it was the original input; explicit --env-file changes that default.
inputs=("${envargs[@]}"); if [[ -z "$orig" && -f .env ]]; then inputs=(--env-file .env); fi
wbase=("${inputs[@]}" --env-file workspace.env -p aob-isolated -f compose.isolated.new.yaml -f compose.phase4-2b-ui.yaml -f compose.workspace-read.yaml)
s1() { docker compose "${wbase[@]}" "$@"; }
s2() { docker compose "${wbase[@]}" -f compose.workspace-azure.yaml --profile dispatch "$@"; }
mkdir "$bk/staged"
cp "$sec/allowed_users.json" "$sec/streamlit-secrets.toml" "$bk/staged/"
"${EDITOR:-vi}" "$bk/staged/allowed_users.json" "$bk/staged/streamlit-secrets.toml"
jq -e '.allowed_users|type=="object" and all(.[]; .enabled != true or
  ((.external_sources|type)=="array" and ((.cross_cloud_scopes // []) |
  all(.[]; .!="aob:runs:execute" and .!="aob:deployments:write" and .!="aob:runs:write"))))' \
  "$bk/staged/allowed_users.json" >/dev/null
for f in allowed_users.json streamlit-secrets.toml; do
  install -o root -g 10001 -m 0440 "$bk/staged/$f" "$sec/$f"
done
s1 config --quiet
shape() { jq -S '[.services | to_entries[] | select(.key!="run-dispatcher") |
  {service:.key,ports:.value.ports,volumes:.value.volumes,networks:.value.networks}]|sort_by(.service)'; }
diff -u <(old config --format json | shape) <(s1 config --format json | shape)
s1 up -d --wait --no-build --pull never --force-recreate control-api event-ingress dashboard
s1 ps
docker exec "$api" python -c 'import os,config as c;assert c.CROSS_CLOUD_ENABLED and not c.REMOTE_RUNS_ENABLED and not c.LEGACY_DEMO_START_ENABLED and os.access(c.ALLOWED_USERS_FILE,os.R_OK);print(c.get_otlp_read_sources())'
docker exec "$ui" python -c 'import streamlit as st,os; a=st.secrets["auth"]; p=a[os.getenv("AOB_AUTH_PROVIDER","auth0")];print(a["redirect_uri"],p["client_kwargs"]["scope"],p["authorize_params"]["audience"]);assert "access" in a["expose_tokens"]'
sqlro 'PRAGMA integrity_check'
sqlro 'SELECT source_id,count(*) FROM deployment_bindings GROUP BY source_id'
history_hash > "$bk/history.after"; diff -u "$bk/history.before" "$bk/history.after"
```

Confirm counts/known IDs against preflight and that both API health checks pass. Persist this exact Compose file order and `--env-file` inputs in the existing operator startup mechanism; replace any old automation which would silently revert the flags. If the stage-1 API list is still 403, inspect **only approved fields** on the API host, not the whole environment:

```bash
read -rp 'Investigated exact subject: ' subject
docker exec "$api" python -c 'import sys,config,demo_auth; r=demo_auth.load_allowed_users().get(sys.argv[1]) or {}; print({k:r.get(k) for k in ("enabled","cross_cloud_scopes","external_sources")},config.get_otlp_read_sources())' "$subject"
```

### Acceptance without leaking user credentials

On the host, use a fresh short-lived **user** token from approved interactive login tooling for the same application, audience and requested scopes. Never enable token logging or create a debug endpoint. Confirm the browser workspace separately after logout/login. `hdr` creates a temporary header file on tmpfs (no secret in curl argv); discard after testing. The optional diagnostic decodes only selected claims **without verification**, not as an authorization decision.

```bash
API="http://$(docker port "$api" 8010/tcp)"; IN="http://$(docker port "$ing" 8011/tcp)"
[[ "$API" == http://127.0.0.1:* && "$IN" == http://127.0.0.1:* ]]
t=$(mktemp -d /dev/shm/aob-auth.XXXXXXXX)
trap 'rm -f "$t/observer" "$t/operator" "$t/denied"; rmdir "$t"' EXIT
hdr() { local bearer; read -rsp "Paste $1 access token (hidden): " bearer; echo
  [[ "$bearer" =~ ^[A-Za-z0-9_.-]+$ ]]; printf 'Authorization: Bearer %s\n' "$bearer" > "$t/$1"; }
hdr observer
python3 - "$t/observer" <<'PY'
import base64,json,sys
p=open(sys.argv[1]).read().strip().split()[2].split('.')[1]
c=json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4)))
print({k:c.get(k) for k in ('iss','aud','sub','exp','scope','permissions')})
PY
get() { curl -q --noproxy '*' -fsS -H "@$t/$1" "$API$2"; }
status() { curl -q --noproxy '*' -sS -o /dev/null -w '%{http_code}' "$@"; }
get observer /api/me | jq -e '.can_start_demo == false'
get observer '/api/v1/deployments?workspace=true&limit=100&offset=0' | jq -e '.items|type=="array"'
get observer '/api/v1/runs?limit=20&offset=0' | jq -e '(.items|type)=="array" and .total>=0'
get observer /api/v1/observations/count | jq '.span_count'
test "$(status "$API/api/v1/runs")" = 401
test "$(status "$IN/api/v1/runs")" = 404
test "$(status -X POST "$IN/v1/traces")" = 401
hdr denied  # valid same-audience user NOT enabled locally
test "$(status -H "@$t/denied" "$API/api/v1/runs")" = 403
read -rp 'Known inaccessible deployment ID: ' otherdep
read -rp 'Known inaccessible local binding (other source, same owner where available): ' other
test "$(status -H "@$t/observer" "$API/api/v1/deployments/$otherdep")" = 404
test "$(status -H "@$t/observer" "$API/api/v1/runs/$other/observations/summary")" = 404
read -rp 'Known authorized historical local binding WITH spans: ' historical
get observer "/api/v1/runs/$historical/observations/summary" | jq -e '.span_count>0'
get observer "/api/v1/runs/$historical/observations/timeline?limit=1" | jq -e '.items|length==1'
read -rp 'Known authorized local binding WITHOUT telemetry: ' bare
get observer "/api/v1/runs?search=$bare" | jq -e --arg id "$bare" 'any(.items[];.run_id==$id)'
get observer "/api/v1/runs/$bare/observations/summary" | jq -e '.span_count==0'
# Valid request shape, but a non-resource ID: even unexpected authorization cannot dispatch.
test "$(status -H "@$t/observer" -H 'Idempotency-Key: observer-denial' -H 'Content-Type: application/json' \
  -d '{"agent_deployment_id":"not-a-real-deployment","workload_version":"synthetic-intake-v1"}' "$API/api/v1/runs")" = 403
```

For a continued 403 use `curl -q --noproxy '*' -sS -H "@$t/observer" "$API/api/v1/runs" | jq '{code,detail}'`. Validate known forbidden records using independent operator inventory, not random IDs. If a historical fixture does not exist, record that acceptance item as **not demonstrated**; do not synthesize records in the production volume. Check Overview, Agent Registry, Run History and historical Agent Observability in the UI; a zero count does not demonstrate real telemetry. Confirm existing public routing does not forward private API requests (including POSTs); do not publish the control service to test it.

## 5. Stage 2: one approved existing Azure agent only

**Gate before enabling either process.** Keep Stage 1 if any item is unknown:

* Confirm with the resource owner a single already-running approved Azure revision/replica implementing the fixed synthetic workload, no scale-up, cold start, paid inference, GPU or incremental charges. Review the [Azure inventory and pin checks](phase4.2/phase4_2B1_deployment_readiness_report.md#azure-inventory-gate-read-only-not-executed). Use its read-only Azure management queries with approved IDs if OCI has no record; DNS alone does not approve a target. No provisioning, secret regeneration, revision/traffic or agent changes.
* Verify existing Azure `RUN_API_TOKEN`, `OTLP_SOURCE_TOKEN`, exporter configured for the **existing** HTTPS OTLP ingress `/v1/traces`, and completion `/v1/traces/complete` (`AOB_OTLP_FINALIZE=1` for the sample). If this requires an Azure or proxy change, **stop**. Authenticated OTLP is HTTP/protobuf; the ingress derives source exclusively by matching the bearer to its JSON map. Invalid/missing/duplicate token config fails closed; no client-specified source or Auth0 scope authorizes ingestion.
* Confirm an existing **registered** Azure **agent**, owned by the execution user's exact `sub`, non-null approved source, `public_https`, `execution_policy=execute`, `start` **and** `status`, `ref:azure-agent`, `ref:azure-run`. Use `GET /api/v1/deployments/{id}`; validation tests metadata, not reachability. If legacy telemetry exists without this deployment it cannot prove a target is approved. Stop for explicit registry-owner registration via the private API and operator approval of its generated ID; do not insert rows, reuse an unrelated ID or change ownership.
* All legacy runs inactive, `run_dispatch` has **no** nonterminal rows including uncertain/parked; no concurrent starts, no other worker. The worker can immediately consume old reservations on startup. Enforce network policy **before starting it**: dedicated bridge, deny host, metadata, lateral, IPv6 and all other external destinations; permit only approved numeric IPv4 TCP/443. A Compose bridge alone does not enforce this. Obtain evidence from the host network owner; no blind firewall edits. Connections use that pin with hostname TLS verification/SNI/Host, no redirects or ambient proxy. `HOST` is a lowercase bare DNS name, no scheme/path/port, IP must be globally routable unicast IPv4, not Azure platform address `168.63.129.16`.

Save Stage 1 files, add only the execution owner's grant in Auth0 and local policy, append `aob:runs:execute` to requested scopes as described above. Keep a distinct observe-only account. Use the secret manager to provision separate protected files: **azure-run-token** plain ASCII printable non-space 32..4096 chars (optional trailing newline); **otlp-tokens.json** e.g. `{"<existing source ID>":"<matching independent producer bearer>"}` preserving *all* existing authorized producers. Values must be nonempty, ASCII and unique. Neither credential is an Auth0 access token. No bearer values in `.env` or flags. If the old Compose has `${AOB_OTLP_SOURCE_TOKENS:?…}`, it is interpolated **before** merge: retain the protected original env input for rollback, or use literal `{}` as a nonempty interpolation value after migrating all credentials to file. Stage 2 overwrites that variable with an empty string inside ingress.

```bash
cp -a "$sec/allowed_users.json" "$sec/streamlit-secrets.toml" "$bk/staged/" # accepted Stage 1
read -rp 'Approved deployment ID: ' dep; read -rp 'Approved source ID: ' src
read -rp 'Approved Azure DNS hostname: ' host; read -rp 'Verified public IPv4 pin: ' ip
[[ "$dep" =~ ^[A-Za-z0-9_.-]+$ && "$host" =~ ^[a-z0-9.-]+$ && "$ip" =~ ^[0-9.]+$ ]]
jq -e --arg s "$src" 'index($s)!=null' <<<"$sources" >/dev/null
printf 'WORKSPACE_DEPLOYMENT=%s\nWORKSPACE_HOST=%s\nWORKSPACE_IPV4=%s\n' "$dep" "$host" "$ip" >> workspace.env
read -rp 'Execution owner sub: ' owner
jq -e --arg u "$owner" --arg s "$src" \
  '.allowed_users[$u] | .enabled==true and (.external_sources|index($s)!=null)' "$sec/allowed_users.json" >/dev/null
jq --arg u "$owner" '.allowed_users[$u].cross_cloud_scopes |= (. + ["aob:runs:execute"] | unique)' \
  "$sec/allowed_users.json" > "$bk/operator.json"
install -o root -g 10001 -m 0440 "$bk/operator.json" "$sec/allowed_users.json"
"${EDITOR:-vi}" "$sec/streamlit-secrets.toml" # append execute to client_kwargs.scope only
read -rp 'Protected secret-manager export directory: ' vault
for f in azure-run-token otlp-tokens.json; do install -o root -g 10001 -m 0440 "$vault/$f" "$sec/$f"; done
python3 - "$sec" "$src" <<'PY'
import json,pathlib,re,sys
p=pathlib.Path(sys.argv[1]); m=json.loads((p/'otlp-tokens.json').read_text())
r=(p/'azure-run-token').read_bytes().rstrip(b'\r\n').decode('ascii')
assert re.fullmatch('[!-~]{32,4096}',r) and sys.argv[2] in m
assert all(k and isinstance(v,str) and v and v.isascii() for k,v in m.items())
assert len(set(m.values()))==len(m) and r not in m.values()
PY
# Require that the proposed map also keeps existing working producers; no secrets to stdout.
docker exec "$ing" python -c 'import config,json;print(json.dumps(config.get_otlp_source_tokens()))' |
  python3 -c 'import sys,json;old=json.load(sys.stdin);new=json.load(open(sys.argv[1]));assert all(new.get(k)==v for k,v in old.items())' "$sec/otlp-tokens.json"
s2 config --quiet
diff -u <(s1 config --format json | shape) <(s2 config --format json | shape)
# Refresh the user/Streamlit mounts while admission is STILL OFF.
s1 up -d --wait --no-build --pull never --force-recreate control-api dashboard
# Recreate ingress for the new file, independent of enabling the run API.
s2 up -d --wait --no-build --pull never --no-deps --force-recreate event-ingress
docker exec "$ing" python -c 'import config,sys; assert sys.argv[1] in config.get_otlp_source_tokens() and not config.OTLP_SOURCE_TOKENS' "$src"
hdr operator  # fresh interactive login after new scope request and RBAC assignment
get operator /api/me | jq -e --arg o "$owner" '.user_id==$o and .can_start_demo==false'
get operator "/api/v1/deployments/$dep" | jq -e --arg s "$src" \
  '.provider=="azure" and .runtime_type=="agent" and .status=="registered" and .source_id==$s and
   .execution_policy=="execute" and .connection_mode=="public_https" and .endpoint_ref=="ref:azure-agent" and
   .credential_ref=="ref:azure-run" and (.capabilities|index("start")!=null and index("status")!=null)'
# Worker preflight with no worker loop and no provider calls. Tests mount permissions/target/pin.
s2 run --rm --no-deps --pull never -T run-dispatcher python -c 'import os,sys,time,config,demo_auth
from crosscloud.policy import Principal
from crosscloud.registry import scoped_deployment
from crosscloud.providers import check_run_capability
from crosscloud.store import reader
from crosscloud.resolution import approved_target,resolve
u=sys.argv[1]; p=Principal.from_verified({"sub":u,"scope":"aob:runs:execute"},demo_auth.load_allowed_users().get(u),config.get_otlp_read_sources())
with reader() as db:
 d=scoped_deployment(db,p,os.environ["AOB_AZURE_RUN_DEPLOYMENT_ID"]); check_run_capability(p,d)
 assert not db.execute("SELECT 1 FROM run_dispatch WHERE state!=?",("terminal",)).fetchone()
 assert not db.execute("SELECT 1 FROM demo_runs WHERE status IN (?,?,?)",("submitting","queued","running")).fetchone()
resolve(d,approved_target(d),time.time()+10); print("worker preflight OK; no send")' "$owner"
# Keep user traffic fenced except the authorized tester for the whole window.
s2 up -d --wait --no-build --pull never control-api dashboard run-dispatcher
s2 ps
w=$(s2 ps -q run-dispatcher); test -n "$w"
docker inspect -f '{{.State.Running}} {{json .NetworkSettings.Networks}} {{json .HostConfig.PortBindings}} {{range .Mounts}}{{println .Destination .RW}}{{end}}' "$w"
```

Verify one running worker on its own bridge, only volume RW, no published ports, no ingress token mounted on control/worker and no run bearer on dashboard/control/ingress. Check mounts with `docker inspect -f '{{range .Mounts}}{{println .Destination .RW}}{{end}}' CONTAINER`. Do not add `KAGGLE_CONFIG_DIR`, Kaggle approval or other provider credentials to the worker. Check denied observer POST again with the **non-resource** ID in Stage 1. If worker/preflight fails, close the window; don't queue a run for later.

### Single acceptance submission (choose CLI **or** UI, not both)

The following is the **one** billable-risk action. Start only after explicit cost/authorization clearance. Use a fresh private receipt directory; persist the key **before** POST. On lost response inspect history and operation state; do not generate another key or repeat through the UI. A 202 is only a local reservation.

```bash
run="$bk/one-azure-run"; mkdir "$run"   # fails on repeat
python3 -c 'from uuid import uuid4;print(uuid4())' > "$run/key"
jq -n --arg d "$dep" '{agent_deployment_id:$d,workload_version:"synthetic-intake-v1",deadline_seconds:120}' > "$run/body"
code=$(curl -q --noproxy '*' -sS --max-time 20 -H "@$t/operator" \
  -H "Idempotency-Key: $(cat "$run/key")" -H 'Content-Type: application/json' \
  --data-binary "@$run/body" -o "$run/receipt" -w '%{http_code}' "$API/api/v1/runs")
test "$code" = 202; local_id=$(jq -er '.run_id|select(type=="string" and length>0)' "$run/receipt")
for ((i=0;i<32;i++)); do
  get operator "/api/v1/runs/$local_id" > "$run/status"
  if jq -e '.dispatch.state=="terminal"' "$run/status" >/dev/null; then break; fi
  sleep 4
done
jq -e --arg d "$dep" --arg s "$src" '.agent_deployment_id==$d and .source_id==$s and
  .dispatch.state=="terminal" and .dispatch.attempts==1 and .business_status=="complete"' "$run/status"
remote=$(jq -er '.remote_run_id' "$run/status")
get operator "/api/v1/runs/$local_id/observations/summary" | jq -e --arg s "$src" --arg r "$remote" \
  '.source_id==$s and .run_id==$r and .completed==100 and .failed==0 and .span_count>0 and .trace_status=="TRACE_COMPLETE"'
get operator "/api/v1/runs/$local_id/observations/units" | jq -e \
  '([.items[].unit_id]|unique|length)==100 and ([.items[].worker_id]|unique|length)==4'
```

Dispatcher sends exactly the sample contract (100 tasks, concurrency 4); `terminal/complete` requires a matching remote ID, total/completed 100 and failed 0. Evidence arrives separately; if delayed, retry **only the GETs**. A binding's `telemetry_status` alone is not proof. Corroborate one POST in existing Azure request logs and the source/remote pair, timeline and denied other-source read; never log headers. No remote cancel. On timeout/uncertain state fence starts, drain dispatcher, retain the binding/slot and reconcile read-only against its original snapshot using the [operator procedure](phase4.2/phase4_2B1_deployment_readiness_report.md); never reset a send right or start again to obtain a green result.

## 6. Rollback preserving history

**Preferred rollback: close execution, keep Stage 1 and the same live data.** Fence all callers first. Stop issuing new reservations before draining the worker. Stage 1 ingress may use the old inline credential mapping: confirm it is still valid and unrevoked, otherwise retain the accepted Stage 2 ingress-only configuration; do not revive revoked credentials.

```bash
s1 up -d --wait --no-build --pull never --force-recreate control-api dashboard
s2 stop -t 100 run-dispatcher
docker inspect -f '{{.State.Status}} {{.State.ExitCode}}' "$w"
s2 rm -f run-dispatcher                  # stopped container only; no volume option
for f in allowed_users.json streamlit-secrets.toml; do
  install -o root -g 10001 -m 0440 "$bk/staged/$f" "$sec/$f"
done
s1 up -d --wait --no-build --pull never --force-recreate control-api event-ingress dashboard
sqlro 'PRAGMA integrity_check'
sqlro 'SELECT state,count(*) FROM run_dispatch GROUP BY state'
```

Revert the Auth0 execute assignment and reauthenticate. Reapply Stage 1 tests. Ensure the startup mechanism now uses **only** the read overlay. If an issue occurred during Stage 1 and the entire old release is needed, keep callers fenced: old code again permits legacy start. Do **not** undo revocations or restore another user's rights; review old files/permissions first. Original Compose files/inputs were not edited. With worker absent:

```bash
for f in allowed_users.json streamlit-secrets.toml; do cp -a "$bk/secrets/$f" "$sec/$f"; done
docker compose "${base[@]}" -f "$bk/images.yaml" up -d --wait --no-build --pull never \
  --force-recreate control-api event-ingress dashboard
old ps
sqlro 'PRAGMA integrity_check'
```

Keep the additive DB, subsequent records, original ports and project. Record rollback image aliases/file order in operations configuration. If storage is damaged, do **not** overwrite the original volume or restore an old snapshot into a dispatch-capable stack. Snapshot current state first; rehearse recovery read-only on a **fresh** named volume:

```bash
(cd "$bk"; sha256sum -c volume.sha256)
clone="aob-restore-check-$(date +%s)"
if docker volume inspect "$clone" >/dev/null 2>&1; then exit 1; fi
docker volume create "$clone"
docker run --rm -i --pull=never --network none -u 0 -v "$clone:/restore" "$helper" \
  tar --numeric-owner -C /restore -xpf - < "$bk/volume.tar"
docker run --rm --pull=never --network none -v "$clone:/restore" "$helper" python -c \
 'import pathlib,sqlite3; p=pathlib.Path("/restore"); assert (p/"demo.db").is_file()
for f in p.glob("*.db"):
 c=sqlite3.connect(f.as_uri()+"?mode=ro",uri=True); assert c.execute("pragma integrity_check").fetchone()==("ok",);print(f.name,"ok"); c.close()'
```

Do not promote the clone as a shortcut: reconcile post-backup records, send rights and remote outcomes first. Recovery that discards subsequent history is **not** a valid rollback. Never use `down -v`, volume pruning, SQL updates or dispatcher lock deletion.

## 7. Source and verification record

* No changes to JWT verification, principal intersection, queries, registry, UI layout or Azure agent. The [login configuration example](../.streamlit/secrets.toml.example) now requests read scopes and explicitly passes the API audience on authorization. Actual OCI secrets are out of tree.
* The only application change is the opt-out gate for legacy demo start and its `/api/me` capability flag (default preserves existing installations), with a denial/no-run regression test in [test_demo_api.py](../tests/test_demo_api.py). The [read overlay](../deploy/oci/compose.workspace-read.yaml) and [Azure overlay](../deploy/oci/compose.workspace-azure.yaml) are the complete Compose deltas.
* `opentelemetry-proto==1.45.2` is already direct in both dependency files; the [Dockerfile](../Dockerfile) installs `requirements.lock` and has build-time protobuf and ASGI import checks. Locally built Linux **amd64** production image: imported `ExportTraceServiceRequest`, `ExportTraceServiceResponse`, both ASGI apps and dispatcher with `--network none`; distribution version 1.45.2. Not a verification of the operator's ARM64 tag: the native-image command above is mandatory.
* Local Compose merge used the checked-in isolated base and dummy env inputs; the pinned Streamlit/Authlib stack generated the expected audience and all four read scopes offline. Production routing, mounted secrets, signed user tokens, real stored history, source grants, pin, cost and live Azure acceptance remain host/operator verification items.
* Full AOB regression suite: **167 passed, 0 skipped, 1 Starlette TestClient deprecation warning** (60.56 s). Ran `python -m pytest -q -ra -p no:cacheprovider` inside a local network-disabled container with the sibling sample's separate Python interpreter and loopback only. The first run lacked the sibling test-only HTTP client; adding the locked `httpx`/`httpcore` to the disposable test interpreter resolved its two failures without changing either application's dependencies. Coverage includes signed JWT denials, same remote ID in two sources, bindings without spans, observer denial, actual loopback sample dispatch/export and Streamlit login/navigation. No live OCI/Azure validation was attempted.
