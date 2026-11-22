# Oracle Cloud ARM64 deployment

This Compose project runs the two existing FastAPI entrypoints. Cloudflare Pages
continues to host the React dashboard, and the separate MarcheAgents BFF remains
responsible for browser sessions and forwarding an Auth0 access token intended
for the AOB audience.

## Prerequisites

1. Install Docker Engine with Compose v2 and enable ARM64 builds on the Oracle
   host.
2. Ensure the existing Caddy deployment owns the public HTTPS routes. Route
   `/api/events` to `event-ingress:8011`; do not add another Caddy container.
3. Create or verify the external network used by the BFF:

   ```sh
   docker network create marcheagents-edge 2>/dev/null || true
   docker network create aob_control 2>/dev/null || true
   ```

   Attach the BFF Compose project to the external `aob_control` network so it
   can resolve `http://control-api:8010`.
4. Put the event secret, `allowed_users.json`, and Kaggle `kaggle.json` on the
   host outside the repository. The Kaggle file is consumed non-interactively
   through `KAGGLE_CONFIG_DIR=/run/secrets`.
5. Copy `.env.example` to `.env`, replace every example value, and confirm the
   event secret file is readable by Docker.

## Build and run

```sh
docker compose -f deploy/oci/compose.yaml build --platform linux/arm64
docker compose -f deploy/oci/compose.yaml up -d
docker compose -f deploy/oci/compose.yaml ps
```

The services do not publish host ports. The BFF reaches `control-api:8010`
over `aob_control`; Caddy reaches `event-ingress:8011` over
`marcheagents-edge`. SQLite data is stored in the `aob-data` named volume,
which is mounted by both services and must not be removed during upgrades.

Never put credentials, the whitelist, or the event secret in this repository.

## Isolated local/staging packaging

`compose.isolated.yaml` is a separate staging configuration for the current
Streamlit, FastAPI, and OTLP application. It does not attach to the production
`aob_control`, `marcheagents-edge`, or `aob_aob-data` resources and does not
modify Caddy. It publishes only loopback ports for SSH-tunnel validation:

```text
127.0.0.1:18501 -> Streamlit
127.0.0.1:18010 -> private API
127.0.0.1:18011 -> event/OTLP ingress
```

Validate the file without starting services:

```sh
docker compose --project-name aob-isolated \
  -f deploy/oci/compose.isolated.yaml config --quiet
```

### Host secrets directory

The isolated Compose file reads these four host files from `AOB_SECRETS_DIR`:

```text
event-secret
allowed_users.json
kaggle.json
streamlit-secrets.toml
```

The default remains `/srv/aob-isolated/secrets` for OCI/Linux. Docker Desktop
users must set the variable to a shared local directory, using Compose path
syntax such as `E:/AOB-Local-Secrets`; do not use the OCI path on Windows:

```powershell
Copy-Item deploy/oci/.env.isolated.example deploy/oci/.env.isolated.local
# Edit .env.isolated.local:
# AOB_SECRETS_DIR=E:/AOB-Local-Secrets
docker compose --env-file deploy/oci/.env.isolated.local `
  --project-name aob-isolated -f deploy/oci/compose.isolated.yaml config --quiet
```

The files are mounted by Docker as container secrets at the unchanged paths
`/run/secrets/aob_event_secret`, `/run/secrets/allowed_users.json`,
`/run/secrets/kaggle.json`, and `/run/secrets/streamlit_config`. Keep the host
directory outside the repository and use dummy values/files for local smoke
tests; never copy production credentials into it.

### Phase 2 staging mode

Set `AOB_ENV=staging` for the isolated OCI staging project. This is not a
development bypass: startup still requires a non-empty Auth0 domain and
audience, a readable whitelist, and a non-empty mounted HMAC secret. It also
requires the exact internal event URL
`http://event-ingress:8011/api/events`; arbitrary HTTP hosts, loopback URLs,
queries, and public event URLs are rejected. The control API joins the
isolated edge network so it can resolve `event-ingress`; the production
Compose networks and Caddy are unchanged.

For the OCI upgrade, copy the new image to the authorized host, update only
the isolated staging env file and `compose.isolated.yaml`, then run:

```sh
docker compose --project-name aob-isolated \
  -f deploy/oci/compose.isolated.yaml config --quiet
docker compose --project-name aob-isolated \
  -f deploy/oci/compose.isolated.yaml up -d --no-build
docker compose --project-name aob-isolated \
  -f deploy/oci/compose.isolated.yaml ps
```

Verify `/healthz` on the two loopback tunnel ports and send a signed synthetic
event plus an OTLP request with a staging-only token. Do not set
`AOB_EVENTS_URL` to `events.marcheagents.com`, do not attach the project to
production networks, and do not run `down -v`.

The attached Phase 1 implementation document explicitly stops after local
packaging and validation; it does not authorize OCI deployment or changes to
the existing production Compose projects.
