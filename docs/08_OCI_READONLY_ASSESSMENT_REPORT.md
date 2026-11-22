# OCI Read-Only Infrastructure Assessment Report

**Assessment date:** 2026-11-22  
**Repository:** `agent-ops-board`  
**Assessment type:** Read-only infrastructure and deployment readiness review  
**Change status:** No OCI services were modified, restarted, stopped, removed, or reconfigured.

## 1. Scope and safety boundary

The assessment used the existing local SSH configuration and key material. Private SSH keys were not copied, displayed, or transferred. The OCI host was queried only for:

- Host identity, architecture, resources, and listening sockets
- Docker Engine and Compose versions
- Running Compose projects and containers
- Docker networks, network attachments, and persistent volumes
- Existing Caddy reverse-proxy metadata and routing shape
- Existing MarcheAgents services and shared networks
- Current AOB image contents and health endpoints

No deployment, image build, Compose lifecycle operation, firewall change, Caddy change, secret read, or filesystem mutation was performed on the host.

## 2. Host inventory

| Item | Observed value |
|---|---|
| Hostname | `instance-20260811-1210` |
| User | `ubuntu` |
| OS/kernel | Ubuntu 22.04, Oracle kernel `6.8.0-1067-oracle` |
| Architecture | `aarch64` |
| CPU | 4 vCPU |
| Memory | 23 GiB total, approximately 22 GiB available |
| Swap | None |
| Root filesystem | 45 GiB total, 25 GiB available, 46% used |
| Docker Engine | 29.7.2 |
| Docker Compose | v5.4.0 |
| Docker images | 18 total, 6.455 GB |
| Docker build cache | 5.222 GB |

The host has sufficient apparent headroom for an isolated staging deployment, but the absence of swap and the shared single-host failure domain remain operational risks.

## 3. Existing Compose projects

| Project | Compose configuration | State |
|---|---|---|
| `aob` | `/srv/aob/app/deploy/oci/compose.yaml` | `running(2)` |
| `marcheagents-bff` | `/srv/marcheagents-bff/app/deploy/compose.yaml` | `running(1)` |
| `rag-stack` | `/opt/rag-stack/docker-compose.yml` | `running(6)` |
| `business` | `/opt/business/docker-compose.yml` | `running(1)` |
| `busapi` | `/opt/business/busapi/BusAPI/docker-compose.yml` | `running(1)` |
| `do_compliance_agent` | `/home/ubuntu/do_compliance_agent/docker-compose.yml` | `running(1)` |

### Existing AOB services

| Container | Image | State | Resource limit |
|---|---|---|---|
| `aob-control-api-1` | `agent-ops-board:arm64` | Healthy | 768 MB, 1 CPU |
| `aob-event-ingress-1` | `agent-ops-board:arm64` | Healthy | 512 MB, 1 CPU |

The existing AOB project uses the `aob_aob-data` named volume for SQLite data.

### Existing MarcheAgents services

| Container | Image | State |
|---|---|---|
| `marcheagents-bff-bff-1` | `marcheagents-bff:arm64` | Healthy |

The BFF is connected to both `aob_control` and `marcheagents-edge`.

## 4. Docker network and volume assessment

Observed Docker networks include:

- `aob_control`
- `aob_egress`
- `marcheagents-edge`
- `rag-stack_default`
- `business-network`
- `do_compliance_agent_default`

Relevant network attachments:

```text
aob_control:
  marcheagents-bff-bff-1
  aob-control-api-1

marcheagents-edge:
  marcheagents-bff-bff-1
  aob-event-ingress-1
  rag-caddy
```

Relevant persistent volumes:

- `aob_aob-data`
- `marcheagents-bff_sessions`
- `rag-stack_caddy_config`
- `rag-stack_caddy_data`
- `rag-stack_postgres_data`
- `rag-stack_redis_data`

An isolated deployment must not reuse the existing `aob_control`, `marcheagents-edge`, or `aob_aob-data` resources.

## 5. Caddy reverse proxy

Caddy is running as the `rag-caddy` container from the `rag-stack` Compose project. There is no host-level `caddy.service`.

The container publishes:

```text
0.0.0.0:80  -> 80
0.0.0.0:443 -> 443
```

The observed routing shape includes:

```text
dipperwoven.com, www.dipperwoven.com
  /api/* -> rag-agent:8000
  remaining requests -> frontend:80

aob-api.marcheagents.com
  -> marcheagents-bff-bff-1:8020

events.marcheagents.com
  POST /api/events -> aob-event-ingress-1:8011
  remaining requests -> 404
```

The existing Caddy project must not be changed as part of the initial AOB staging deployment. Public routing is a separate approved change.

## 6. Current public/listening surface

Observed listening ports include:

```text
22/tcp       SSH
80/tcp       Caddy
443/tcp      Caddy
5433/tcp     business PostgreSQL, published on all interfaces
127.0.0.1:5432
127.0.0.1:6379
127.0.0.1:8000
127.0.0.1:8001
127.0.0.1:8080
*:11434      owner not identified during this assessment
```

UFW is active with a default deny incoming policy and currently allows:

- SSH 22/tcp from anywhere
- HTTP 80/tcp from anywhere
- HTTPS 443/tcp from anywhere

Docker-published ports must be reviewed independently of UFW because Docker forwarding can bypass assumptions based only on host input rules.

## 7. Current AOB image readiness

The running AOB image is correctly identified as:

```text
OS:           linux
Architecture: arm64
```

Both existing AOB containers reported healthy `/healthz` responses.

However, the running image does not contain the current Streamlit/OTLP application surface. Read-only checks showed the following are missing from the image:

```text
/app/dashboard/app.py
/app/api_client.py
/app/otlp_store.py
/app/otlp_api.py
streamlit package
```

The repository [Dockerfile](../Dockerfile) currently copies the FastAPI-related modules but does not copy the dashboard package or OTLP store module. The current [requirements.lock](../requirements.lock) also does not contain Streamlit and the dashboard runtime dependencies.

This means the requested current Streamlit-based AOB cannot be safely deployed by reusing the existing image. A new image packaging correction and ARM64 build are required.

## 8. Security assessment

### Existing positive controls

- Existing AOB FastAPI services do not publish host ports.
- The production FastAPI applications disable OpenAPI, Swagger, and ReDoc endpoints.
- Secrets are mounted from `/srv/aob/secrets`, outside the repository.
- AOB uses separate control and egress network concepts.
- UFW defaults to deny incoming traffic.
- Existing Caddy is the only service publishing 80/443.

### Findings and recommendations

#### High priority: SSH source restriction

SSH is allowed from anywhere. Restrict port 22 to an administrator fixed `/32`, a controlled VPN range, or OCI Bastion. Password authentication should remain disabled.

#### High priority: published business PostgreSQL

`business-postgres` publishes `0.0.0.0:5433`. Review whether this is required. If it is not required externally, bind it to loopback or remove the publication in a separately approved maintenance change.

#### Medium priority: unidentified port 11434

Identify the process/container owning `*:11434` before production exposure. Confirm whether it is intended to be public and whether it has authentication and rate limiting.

#### Medium priority: no swap

There is no swap configured. Add a carefully sized swap policy only through a separately approved host-maintenance change after confirming OCI disk and operational requirements.

#### Medium priority: shared SQLite write access

The current AOB design allows control and ingress processes to share a SQLite volume. This is acceptable only as an MVP compromise and requires backup, locking, and restore testing.

#### Medium priority: shared host failure domain

The AOB, MarcheAgents, business, and RAG services share one VM and Docker daemon. Resource contention, host failure, or disk loss affects multiple systems.

## 9. Recommended isolated deployment

Deploy the current application as a separate Compose project:

```text
Compose project: aob-isolated
```

Recommended services:

1. `dashboard`
   - Streamlit application
   - Private connection to `control-api`
   - No direct SQLite access
2. `control-api`
   - `api.private_app:app`
   - Internal only
   - Kaggle credentials mounted only here
3. `event-ingress`
   - `api.public_ingress:app`
   - Signed event and OTLP ingestion
   - No public exposure during staging
4. SQLite named volume
   - Unique name: `aob-isolated-data`

Use unique networks:

```text
aob-isolated-control
aob-isolated-edge
aob-isolated-egress
```

Do not connect the new project to:

```text
aob_control
marcheagents-edge
aob_aob-data
rag-stack_default
```

During staging, bind test ports to loopback only:

```text
127.0.0.1:18501 -> Streamlit 8501
127.0.0.1:18010 -> Private API 8010
127.0.0.1:18011 -> Ingress/OTLP 8011
```

This permits validation through an SSH tunnel without changing Caddy or public DNS.

## 10. Exact planned commands

The following commands are the approved implementation sequence, but were **not executed during this assessment**.

### 10.1 Local validation

```powershell
Set-Location E:\AllProjects\2026EmailIntentAI\agent-ops-board

python -m pytest -q

docker buildx build `
  --platform linux/arm64 `
  --tag agent-ops-board:isolate-arm64 `
  --load `
  .

docker image inspect agent-ops-board:isolate-arm64 `
  --format 'architecture={{.Architecture}} os={{.Os}}'
```

Expected image metadata:

```text
architecture=arm64 os=linux
```

### 10.2 Source transfer

Transfer only a source archive. Do not transfer SSH keys or local secret files:

```powershell
Set-Location E:\AllProjects\2026EmailIntentAI\agent-ops-board

git archive `
  --format=tar `
  --output="$env:TEMP\aob-source.tar" `
  HEAD

scp `
  -o IdentitiesOnly=yes `
  -i "$HOME\.ssh\oracle_new" `
  "$env:TEMP\aob-source.tar" `
  ubuntu@137.23.18.237:/tmp/aob-source.tar
```

### 10.3 Release preparation

```powershell
ssh `
  -o IdentitiesOnly=yes `
  -i "$HOME\.ssh\oracle_new" `
  ubuntu@137.23.18.237 `
  "mkdir -p /srv/aob-isolated/releases/<RELEASE_ID> /srv/aob-isolated/secrets"
```

Secrets must be created separately on the host with restricted permissions and must not be committed or printed.

### 10.4 Compose validation

```bash
cd /srv/aob-isolated/releases/<RELEASE_ID>

docker compose \
  --project-name aob-isolated \
  -f compose.isolated.yaml \
  config --quiet
```

### 10.5 Build and start only the isolated project

```bash
docker compose \
  --project-name aob-isolated \
  -f compose.isolated.yaml \
  build --pull

docker compose \
  --project-name aob-isolated \
  -f compose.isolated.yaml \
  up -d
```

The Compose file must contain no references to existing AOB or MarcheAgents networks, volumes, or Caddy configuration.

### 10.6 Verify the isolated project

```bash
docker compose \
  --project-name aob-isolated \
  -f compose.isolated.yaml \
  ps

docker inspect \
  aob-isolated-dashboard-1 \
  aob-isolated-control-api-1 \
  aob-isolated-event-ingress-1 \
  --format '{{.Name}} {{.State.Status}}'
```

### 10.7 SSH tunnel validation

```powershell
ssh `
  -o IdentitiesOnly=yes `
  -i "$HOME\.ssh\oracle_new" `
  -L 8501:127.0.0.1:18501 `
  -L 18010:127.0.0.1:18010 `
  -L 18011:127.0.0.1:18011 `
  ubuntu@137.23.18.237
```

From another local terminal:

```powershell
curl http://127.0.0.1:18010/healthz
curl http://127.0.0.1:18011/healthz
```

Open the Streamlit UI at `http://127.0.0.1:8501` while the tunnel is active.

## 11. Validation gates

Before any public routing change:

- The local test suite passes.
- The image reports `linux/arm64`.
- Compose configuration validates with `config --quiet`.
- Dashboard, private API, and ingress containers are healthy.
- Only loopback test ports are published.
- The isolated project is absent from `aob_control` and `marcheagents-edge`.
- The isolated project does not mount `aob_aob-data`.
- Existing AOB and MarcheAgents health remains unchanged.
- `/docs`, `/redoc`, and OpenAPI endpoints remain unavailable.
- SQLite persistence survives a controlled isolated container recreation.
- Logs contain no access tokens, HMAC secrets, Kaggle credentials, or raw customer data.

Any public deployment should be a separate change with explicit Caddy, DNS, TLS, firewall, and external-network validation.

## 12. Rollback procedure

The isolated rollback must not affect existing Compose projects:

```bash
cd /srv/aob-isolated/releases/<RELEASE_ID>

docker compose \
  --project-name aob-isolated \
  -f compose.isolated.yaml \
  down
```

Do not use `down -v`; preserve the isolated SQLite volume unless data deletion is explicitly approved.

To return to a previous isolated release:

```bash
docker compose \
  --project-name aob-isolated \
  -f /srv/aob-isolated/releases/<PREVIOUS_RELEASE_ID>/compose.isolated.yaml \
  up -d
```

Do not run rollback commands against the existing `aob`, `marcheagents-bff`, or `rag-stack` projects as part of this procedure.

## 13. Assessment conclusion

The OCI host has adequate current resource headroom and an existing working AOB/MarcheAgents/Caddy topology. The safest next step is not to alter that topology, but to first correct the application image packaging and deploy the current Streamlit/OTLP application as a privately reachable, uniquely named Compose project.

Public Caddy integration should be deferred until the isolated deployment passes all validation gates and receives explicit approval for a separate, reversible routing change.
