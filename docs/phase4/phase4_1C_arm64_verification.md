# Gate 2: OCI ARM64 image verification

**Prepared locally; awaiting explicit authorization. No SSH, transfer, build or smoke on OCI has occurred.** Gate 2 is pending, not failed or passed. Gate 1 (five pre-existing staging failures) is **deferred, not passed**. Do not deploy.

## Candidate identity

- AOB `runtime_phase1`, HEAD `c88fe266ebbd6bc41893c7891ca850034c180527` **plus the local working tree**. No staged changes. The remote commit alone is not this candidate.
- `candidate.tar.gz`: **103495 bytes**, SHA-256 **`c51834286e1d0edd52097c0892012c5c5050658e78cd3ea9a1018e3a9c945961`**. All 62 regular files compared byte-for-byte with local sources after packaging. This is the exact **image build context**, not a full checkout: original Dockerfile and `.dockerignore`, both requirements files, the root modules in the Dockerfile's `COPY`, and the tracked Python files under its ten copied directories. No `.git`, env files, credentials, virtualenv, runtime DB, deployments, tests or documentation in the context.
- Dirty build inputs: `.dockerignore`, `Dockerfile`, `config.py`, `demo_api.py`, `otlp_api.py`, `otlp_store.py`, `requirements.lock`, `requirements.txt`, `trace_store.py`, and **untracked `otlp_semantics.py`**. Docker copies the semantics module; the runtime lock pins OTel API, SDK and proto 1.45.2, semantic conventions 0.66b2 and protobuf 6.33.5. The AOB receiver does not need Azure's HTTP exporter.
- Outside the artifact: modified `deploy/oci/{.env.isolated.example,compose.isolated.yaml}`, `tests/{test_otlp_receiver,test_trace_observability}.py`; untracked `tests/{rehearse_phase4_migration,test_azure_otlp_live,test_external_reads}.py`, four Phase A documents, B implementation report, C readiness report and this runbook. See the [release review](phase4_1C_release_readiness.md). No connector files or changes to production services are part of Gate 2.

## Transfer and invocation (only after authorization)

Use an existing SSH alias with a previously verified host key, local Docker socket access and no escalation. The only variable requiring operator input is `OCI`. These are Bash commands **from the local checkout**. The reviewed host program is the single `bash` fence below; extracting it avoids retyping or transferring this report. Use an off-peak window: a native build still competes for host resources. Build requires public base, Debian and PyPI downloads; **smoke requires no network or credentials**.

```sh
set -euo pipefail
: "${OCI:?set OCI to the explicitly authorized SSH alias}"
D=/home/codespace/.copilot/session-state/6dc431c8-6131-46f5-b10d-a4fa6fe785fa/files/gate2
cd /workspaces/agent-ops-board
sed -n '/^```bash$/,/^```$/p' docs/phase4/phase4_1C_arm64_verification.md | sed '1d;$d' > "$D/verify.sh"
cd "$D"
sha256sum -c candidate.sha256
bash -n verify.sh
opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ForwardAgent=no)
REMOTE=$(ssh "${opts[@]}" "$OCI" 'umask 077; mktemp -d /tmp/aob-arm64-gate2.XXXXXXXX')
[[ $REMOTE =~ ^/tmp/aob-arm64-gate2\.[[:alnum:]]{8}$ ]]
scp "${opts[@]}" candidate.tar.gz verify.sh "$OCI:$REMOTE/"
ssh "${opts[@]}" "$OCI" "cd '$REMOTE' && bash verify.sh" 2>&1 | tee "$D/host.log"
```

## Exact host program (not executed)

Stop rather than installing tools, pruning, or redirecting to another daemon if any prerequisite fails. Conservative preflight floors: 10 GiB free on Docker's storage filesystem and 2 GiB available RAM; these are minima, not guarantees. Check the containerd filesystem too if it is separate. Use existing system Docker/buildx, host Python 3, and an empty client config (no registry login). No bind mount, existing volume, production network, published port, Compose, SSH agent forwarding or registry push.

```bash
set -euo pipefail
umask 077
test "$(uname -s)" = Linux
echo "host arch: $(uname -m)"
test "$(uname -m)" = aarch64
printf '%s  %s\n' c51834286e1d0edd52097c0892012c5c5050658e78cd3ea9a1018e3a9c945961 candidate.tar.gz | sha256sum -c -
mkdir context docker-client
unset DOCKER_CONTEXT BUILDX_BUILDER DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock DOCKER_CONFIG="$PWD/docker-client"
root=$(docker info --format '{{.DockerRootDir}}')
check_disk() {
  df -h "$1"
  test "$(df -Pk "$1" | awk 'END {print $4}')" -ge 10485760
}
check_disk "$root"
if test -d /var/lib/containerd; then check_disk /var/lib/containerd; fi
df -h .
free -h
awk '/^MemAvailable:/ {ok=($2>=2097152)} END {exit !ok}' /proc/meminfo
docker buildx inspect default
tar --extract --gzip --file candidate.tar.gz --directory context --no-same-owner
tag="aob-gate2:c51834286e1d-${PWD##*.}"
tag=${tag,,}
if docker image inspect "$tag" >/dev/null 2>&1; then echo "Tag occupied; stop" >&2; exit 1; fi
echo "isolated tag: $tag"
BUILDX_METADATA_PROVENANCE=max docker buildx build --builder default --platform linux/arm64 \
  --progress=plain --metadata-file build.json --load -t "$tag" context 2>&1 | tee build.log
id=$(docker image inspect --format '{{.Id}}' "$tag")
platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$id")
echo "build=ok image=$id platform=$platform"
test "$platform" = linux/arm64
python3 - <<'PY'
import json
m = json.load(open("build.json"))["buildx.build.provenance"]["materials"]
base = [x for x in m if x["uri"].startswith("pkg:docker/") and "python" in x["uri"]]
assert len(base) == 1 and len(base[0]["digest"]["sha256"]) == 64, m
print("base material:", base[0]["uri"], "sha256:" + base[0]["digest"]["sha256"])
PY
docker run --rm -i --pull=never --platform linux/arm64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,nosuid,nodev,size=96m,mode=1777 \
  -e AOB_RUNTIME_DIR=/tmp/runtime -e AOB_DEMO_DB_PATH=/tmp/runtime/demo.db "$id" python - <<'PY'
import os, platform, sqlite3, sys
from pathlib import Path
import fastapi, streamlit, uvicorn, otlp_semantics, otlp_api, otlp_store
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from api.private_app import app as private
from api.public_ingress import app as ingress
from fastapi.testclient import TestClient
assert os.getuid() == 10001 and platform.machine() == "aarch64"
print("uid=", os.getuid(), "python=", sys.version.split()[0], "imports=ok")
p = Path(os.environ["AOB_DEMO_DB_PATH"])
assert not p.exists()
for name, app in (("private", private), ("public", ingress)):
    with TestClient(app) as client:
        r = client.get("/healthz")
        print(name, "/healthz", r.status_code, r.json())
        assert r.status_code == 200 and r.json() == {"status": "ok"}
assert p.is_file()
with sqlite3.connect(p.as_uri() + "?mode=ro", uri=True) as db:
    assert db.execute("pragma integrity_check").fetchone() == ("ok",)
    assert "start_time_unix_nano" in {r[1] for r in db.execute("pragma table_info(external_agent_spans)")}
print("ephemeral SQLite=ok; ARM64 smoke=PASS")
PY
```

`TestClient` exercises the real ASGI routes and startup handlers without opening a listening socket. No production-mode auth settings are supplied or tested. The smoke container and tmpfs disappear on exit. Build cache, isolated image tag and private temporary directory are left for evidence; if cleanup is authorized, remove only this run's tag and named files/directory after retaining evidence, never shared cache or volumes.

## Evidence pending execution

| Field | Observed on OCI |
|---|---|
| Host arch, Docker storage headroom, available RAM | Not measured |
| Base material digest, image ID, image OS/arch, build exit status | Not measured |
| Runtime UID, imports, private/public HTTP status + JSON, SQLite integrity | Not measured |
| Failures or missing host dependencies | Unknown; no attempt |

Attach `host.log` and record the actual fields above after the authorized run. Missing provenance, missing headroom or an incomplete smoke is **not a pass**. Even a successful run would clear only Gate 2; deployment remains a separate decision.
