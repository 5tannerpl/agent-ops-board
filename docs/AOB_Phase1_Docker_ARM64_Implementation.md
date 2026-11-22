# AOB Phase 1 — Docker Image Packaging & ARM64 Validation

**Date:** 2026-11-22  
**Repository:** `agent-ops-board`  
**Scope:** Local code and packaging only. **NO OCI changes and NO deployment.**

## Goal

Repair the current AOB image so that it can run the existing **Streamlit Dashboard**, **FastAPI private control API**, **FastAPI public event/OTLP ingress**, and OTLP data handling on **linux/arm64**. Prepare, but do not deploy, a new isolated Compose configuration. Keep all current production AOB and MarcheAgents deployment files and resources untouched.

## Important input

The existing OCI assessment found the running image did not contain:

- `/app/dashboard/app.py`
- `/app/api_client.py`
- `/app/otlp_store.py`
- `/app/otlp_api.py`
- `streamlit` Python package

Its existing Dockerfile and `requirements.lock` primarily package the current FastAPI runtime. These facts describe the assessed image; verify the **actual working tree** before editing. Do not assume all paths, import locations or API signatures match earlier versions.

## Agent instructions (execute in order)

### 0. Inventory before changes

1. Record `git status --short`, active branch and HEAD SHA. Do not discard uncommitted work.
2. Inspect `Dockerfile`, `requirements*.txt`, `requirements.lock`, `pyproject.toml` or other dependency management, `.dockerignore`, `dashboard/`, `api/`, `api_client.py`, `otlp_store.py`, `otlp_api.py`, `config.py`, the current Compose files and tests.
3. Resolve all actual Python imports reachable by all three entrypoints; look for templates, static files, assets and native libraries in addition to `.py` files.
4. Locate and document actual CLI commands, app object exports, health endpoints, environment variables, path assumptions, secrets and volume requirements.
5. Assess architecture compatibility of binary wheels for linux/arm64 and Python version. Avoid adding amd64-only base layers or wheel requirements.
6. Report findings before implementing if there are incompatible or missing source modules.

### 1. Make Dockerfile package the complete app

1. Use an ARM64-compatible Python runtime and existing project base-image pinning policy.
2. Include all required source modules, dashboard pages/assets, API modules and runtime resources in the image. Do not broadly `COPY .` without reviewing `.dockerignore` for secrets and local files.
3. Keep the container non-root, set predictable working directory and import path, and preserve currently required environment defaults and health behavior.
4. Ensure runtime installs all **locked** dependencies, including Streamlit and OTLP dependencies. Resolve missing dependencies based on actual imports, not guesses.
5. Use the same built image for the three services unless justified by actual dependencies. Each service should override its command in Compose rather than requiring three divergent Dockerfiles.
6. Do not copy `.env`, `.streamlit/secrets.toml`, `allowed_users.json`, credentials, SSH keys, SQLite files, notebooks or local caches into an image.
7. Preserve existing production behavior of the public/private FastAPI apps and their endpoint security settings.

### 2. Lock dependencies reproducibly

1. Find the current canonical dependency declaration and lock generation process.
2. Add all needed Streamlit, API and OTLP packages with compatible pinned versions; don't blindly upgrade unrelated packages.
3. Regenerate the lock using the project's established method; record the exact command used.
4. Validate dependency installation for ARM64 (prefer linux/arm64 build and install verification). Confirm no x86-only dependencies slipped in.
5. Record whether the project uses multi-platform Buildx/QEMU and whether the local Docker engine supports `--load` on this build setup.

### 3. Create an *isolated* staging Compose file

Create `compose.isolated.yaml` (or another clearly named source-controlled file), *without changing* the production Compose file. Services:

- `dashboard`: Streamlit UI, listens internally at `8501`, calls private control API over isolated control network, **no direct SQLite volume mount**.
- `control-api`: actual `api.private_app:app` entrypoint or verified equivalent; port `8010`; mounts its required, isolated SQLite volume; Kaggle credentials only if required, via host-mounted secrets, never baked into image.
- `event-ingress`: actual `api.public_ingress:app` or verified equivalent; port `8011`; signed event/OTLP ingestion; isolated persistent SQLite volume if current implementation requires it.

Use project name `aob-isolated` at invocation time; network names must be unique and disjoint from `aob_control`, `aob_egress`, `marcheagents-edge`, and `rag-stack_default`.

Use one uniquely named persistent volume `aob-isolated-data` (unless the actual app requires more). Never reference `aob_aob-data` or current production bind mounts. For staging, publish **only**:

- `127.0.0.1:18501:8501` Dashboard
- `127.0.0.1:18010:8010` private API
- `127.0.0.1:18011:8011` ingress

No Caddy, Cloudflare, DNS, UFW or production network changes. Keep egress only where required. Explicitly consider whether Streamlit's browser-facing URL settings work with an SSH tunnel. Handle shared SQLite concurrency carefully and preserve DB file permissions. No hardcoded auth or signing credentials; use a documented local-only dummy secret for local smoke tests, and a separate secure host-secret procedure for later stages.

**Security:** `docker compose config` must not print secret values in a shared log. Keep OpenAPI/docs disabled in the production configuration. Require signing/auth checks at ingress in staging; do not temporarily open an unauthenticated ingest endpoint.

### 4. Local validation commands

Run the repo's canonical test/lint/typecheck commands. Minimum suggested commands (adjust to actual repository):

```powershell
python -m pytest -q

docker buildx build `
  --platform linux/arm64 `
  --tag agent-ops-board:isolate-arm64 `
  --load `
  .

docker image inspect agent-ops-board:isolate-arm64 `
  --format 'architecture={{.Architecture}} os={{.Os}}'

docker compose `
  --project-name aob-isolated `
  -f compose.isolated.yaml `
  config --quiet
```

If Docker Desktop is not configured for running ARM64 containers, build/inspect can prove packaging but **cannot prove runtime startup**. State this explicitly; use a suitable ARM64 test environment in a separately authorized step, without accessing production OCI resources. `--load` may not work with some multi-platform builders; use an appropriate compatible builder or single-platform load. Do not claim success based only on successful build output.

### 5. Smoke tests and acceptance criteria

Provide an automated smoke-test script and document the invocation. Verify all of the following where the execution environment permits:

1. Image architecture equals `arm64` and OS equals `linux`.
2. Python can import Streamlit and actual dashboard/API/OTLP modules from within the built image.
3. Dashboard starts without missing-module, missing-asset, or path errors; Streamlit HTTP endpoint works.
4. Private API starts and `/healthz` returns a successful response.
5. Ingress starts and `/healthz` returns a successful response.
6. Valid synthetic, correctly signed test event accepted by ingress and reflected through the intended read API; invalid signature rejected. Use actual request schemas.
7. Where implemented, synthetic OTLP payload accepted and trace data retrievable through actual AOB API; no imaginary endpoints.
8. SQLite persists through recreation of **isolated** containers; do not use `down -v`.
9. Compose config validates; bindings are loopback only; no attachment to production networks, volumes or containers.
10. Security checks: no leaked secrets/credentials in image layers, env/log output or Git diff; `/docs`, `/redoc` and OpenAPI routes unavailable in production mode.
11. Existing tests remain passing; new focused unit/integration tests cover entrypoint imports and runtime packaging.

If smoke testing is performed on a shared Docker host, don't stop or restart any existing unrelated container; restrict every lifecycle command to `--project-name aob-isolated` and the new Compose file.

### 6. Explicit prohibitions

- Do **NOT** SSH to OCI, SCP files, create directories or containers on OCI, change Caddy/Cloudflare, publish DNS, alter UFW, remove Docker volumes, touch other Compose projects, or run `docker system prune`.
- Do **NOT** modify the existing `/srv/aob` or `/srv/marcheagents-bff` deployment tree.
- Do **NOT** store passwords, tokens or HMAC secrets in source control or Docker layers.
- Do **NOT** break the existing deployed FastAPI AOB services.

### 7. Required deliverables

- A concise inventory of current entrypoints, imports, runtime variables and corrected packaging deficiencies.
- Patch to Dockerfile, dependency declaration/lock and `.dockerignore` (as actually needed).
- New independent `compose.isolated.yaml` and example of required environment variable *names* only.
- Automated Docker import/start/health/OTLP smoke tests (using actual routes and payloads).
- Test/build transcript with command, exit code, and relevant output; distinguish PASS, FAIL, NOT RUN and BLOCKED.
- List of changed files and `git diff --stat`.
- A short handover for Phase 2 OCI staging. **Stop after local Phase 1 and wait for deployment authorization.**

## Completion gate

Phase 1 is **complete** only when the source patch and tests are actually executed and evidenced. Producing a Compose file or build script without successful tests is **not** completion. Treat untested ARM64 runtime behavior as pending validation rather than passing.
