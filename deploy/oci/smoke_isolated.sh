#!/usr/bin/env sh
set -eu

compose() {
  if [ -n "${AOB_ENV_FILE:-}" ]; then
    docker compose --env-file "$AOB_ENV_FILE" \
      --project-name aob-isolated -f deploy/oci/compose.isolated.yaml "$@"
  else
    docker compose --project-name aob-isolated -f deploy/oci/compose.isolated.yaml "$@"
  fi
}

docker image inspect "${AOB_IMAGE:-agent-ops-board:isolate-arm64}" \
  --format 'architecture={{.Architecture}} os={{.Os}}' |
  grep -Fx 'architecture=arm64 os=linux'

compose config --quiet
compose up -d --no-build

compose run --rm --no-deps --entrypoint python control-api \
  -c 'import importlib; [importlib.import_module(m) for m in ("streamlit", "api.private_app", "api.public_ingress", "otlp_api", "otlp_store")]; print("python imports: ok")'

curl --fail --silent http://127.0.0.1:18010/healthz
curl --fail --silent http://127.0.0.1:18011/healthz

test "$(curl --silent --output /dev/null --write-out '%{http_code}' \
  -X POST -H 'content-type: application/x-protobuf' \
  --data-binary '' http://127.0.0.1:18011/v1/traces)" = 401
