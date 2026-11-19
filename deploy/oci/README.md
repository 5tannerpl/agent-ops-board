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
