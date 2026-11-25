"""Local-only disposable smoke. python tests/smoke_crosscloud_compose.py <local-image-id-or-tag>"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from uuid import uuid4


def smoke(image):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as tmp:
        project = "b1-smoke-" + uuid4().hex
        env = dict(PATH=os.environ["PATH"], HOME=tmp, DOCKER_HOST="unix:///var/run/docker.sock",
                   AOB_AUTH0_DOMAIN="invalid.invalid", AOB_AUTH0_AUDIENCE="test",
                   AOB_STAGING_IMAGE=image, AOB_STAGING_SECRETS_DIR=tmp,
                   AOB_STAGE_CONTROL_PORT="0", AOB_STAGE_DASH_PORT="0", AOB_STAGE_INGRESS_PORT="0")
        for name, value in {"allowed_users.json": '{"allowed_users":{}}',
                            "streamlit-secrets.toml": "# test only", "event-secret": secrets.token_hex(32),
                            "otlp-tokens.json": json.dumps({"test": secrets.token_hex(32)}),
                            "azure-run-token": secrets.token_hex(32)}.items():
            path = Path(tmp, name)
            path.write_text(value)
            path.chmod(0o444)  # Synthetic secrets inside 0700 directory.
        cmd = ["docker", "compose", "--env-file", "/dev/null", "-p", project, "-f",
               str(root / "deploy/oci/compose.crosscloud-staging.yaml")]

        def dc(*args):
            return subprocess.run([*cmd, *args], env=env, check=True, text=True,
                                  stdout=subprocess.PIPE, timeout=150).stdout.strip()

        def check(service, code):
            dc("exec", "-T", service, "python", "-c", code)

        try:
            dc("up", "--wait", "--no-build")
            assert "run-dispatcher" not in dc("ps", "--services").splitlines()
            check("control-api", """import sqlite3,config
assert not config.CROSS_CLOUD_ENABLED and not config.REMOTE_RUNS_ENABLED
c=sqlite3.connect(config.DEMO_DB_PATH); assert not c.execute("select 1 from sqlite_master where name='deployments'").fetchone()
""")
            env["AOB_CROSS_CLOUD_ENABLED"] = "true"
            dc("up", "--wait", "--no-build")
            check("control-api", """import sqlite3,config
assert config.CROSS_CLOUD_ENABLED and not config.REMOTE_RUNS_ENABLED
assert sqlite3.connect(config.DEMO_DB_PATH).execute('select count(*) from dispatch_context').fetchone()==(0,)
""")
            dc("run", "--rm", "--no-deps", "run-dispatcher")  # exits cleanly while off
            env["AOB_REMOTE_RUNS_ENABLED"] = "true"
            dc("--profile", "dispatch", "up", "--wait", "--no-build")
            check("run-dispatcher", """import os,config,sqlite3
from pathlib import Path
assert os.geteuid()==10001 and Path(config.ALLOWED_USERS_FILE).is_file()
assert Path(os.environ["AOB_AZURE_RUN_TOKEN_FILE"]).is_file() and not config.get_otlp_source_tokens()
assert sqlite3.connect(config.DEMO_DB_PATH).execute('select count(*) from dispatch_context').fetchone()==(0,)
""")
            check("event-ingress", """import config
from pathlib import Path
assert set(config.get_otlp_source_tokens())=={'test'} and not Path('/run/secrets/azure_run_token').exists()
""")
            check("dashboard", """from pathlib import Path
assert Path('/app/.streamlit/secrets.toml').is_file() and not Path('/run/secrets/azure_run_token').exists()
""")
            worker = dc("ps", "-q", "run-dispatcher")
            assert worker
            def inspect(fmt):
                return subprocess.check_output(["docker", "inspect", "-f", fmt, worker], env=env, text=True).strip()
            assert list(json.loads(inspect("{{json .NetworkSettings.Networks}}"))) == [project+"_dispatch-egress"]
            assert not json.loads(inspect("{{json .HostConfig.PortBindings}}"))
            mounts = json.loads(inspect("{{json .Mounts}}"))
            assert {m["Destination"] for m in mounts if m["RW"]} == {"/var/lib/aob"}
            dc("stop", "run-dispatcher")
            assert inspect("{{.State.ExitCode}}") == "0"
        finally:
            dc("--profile", "dispatch", "down", "-v")  # only this random project; no external volumes


if __name__ == "__main__":
    smoke(sys.argv[1])
    print("Disposable Compose migration, service boundaries, default-off and shutdown: PASS")
