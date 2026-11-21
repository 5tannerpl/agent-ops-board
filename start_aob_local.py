#!/usr/bin/env python3
"""One-command Windows launcher for the independent local Agent Ops Board.

Starts private Event Ingress (8011), a temporary Cloudflare HTTPS tunnel,
private Control API (8010), and Streamlit Dashboard (8501) in that order.

No secrets are embedded. Existing environment variables take precedence over
an optional, untracked .env.local (or --env-file). Press Ctrl+C to stop.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

TUNNEL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com(?:/)?")
DEFAULT_REF = "sillypanda5/notebookb061a06458"
DEFAULT_PATH = "/api/events"
PROCESS_LOGS = ("INGRESS", "TUNNEL", "CONTROL", "STREAMLIT")


def say(message: str) -> None:
    print(f"[AOB] {message}", flush=True)


def load_env_file(path: Path, env: dict[str, str]) -> None:
    if not path.exists():
        return
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid env-file line {line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid env variable name at line {line_no}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env.setdefault(key, value)  # Explicit process environment wins.
    say(f"Loaded local settings: {path.name} (values not printed)")


# Only explicitly allowlisted non-secret configuration values are printed in clear text.
# All other relevant settings are shown as SET/MISSING to avoid leaking credentials.
VISIBLE_CONFIG_KEYS = frozenset({
    "AOB_OTEL_ENABLED", "AOB_OTEL_EXPORTER", "AOB_OTEL_SERVICE_NAME",
    "AOB_OTEL_ENVIRONMENT", "AOB_OTEL_COMMIT", "AOB_REPO_BRANCH",
    "AOB_MODEL_BACKEND", "AOB_MODEL_NAME", "AOB_KAGGLE_NOTEBOOK_REF",
    "AOB_API_URL", "AOB_EVENTS_URL", "AOB_REPO_URL",
})
CONFIG_PREFIXES = ("AOB_", "KAGGLE_", "AUTH0_", "CLOUDFLARE_", "CF_")
CONFIG_EXTRAS = ("PYTHONPATH", "PYTHONUNBUFFERED", "STREAMLIT_SERVER_PORT")


def _safe_config_value(key: str, value: str) -> str:
    if not value:
        return "MISSING"
    # Unknown settings and auth/credential-related settings are always masked.
    if key not in VISIBLE_CONFIG_KEYS:
        return "SET (redacted)"
    if re.search(r"SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL|COOKIE|AUTH|PRIVATE", key, re.I):
        return "SET (redacted)"
    if key == "AOB_REPO_URL":
        # Userinfo/query strings could contain credentials; don't print the full URL.
        try:
            parsed = urlparse(value)
            return f"{parsed.scheme}://{parsed.hostname or '[unknown host]'}{parsed.path}"
        except ValueError:
            return "SET (redacted)"
    if key in ("AOB_API_URL", "AOB_EVENTS_URL"):
        try:
            parsed = urlparse(value)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return "SET (redacted URL)"
        except ValueError:
            return "SET (redacted URL)"
    return value if len(value) <= 180 and "\n" not in value and "\r" not in value else "SET (redacted)"


def print_effective_config(env: dict[str, str], env_file: Path, py: str, cloudflared: str) -> None:
    say("========== Effective AOB startup configuration ==========")
    say(f"Environment file: {env_file} ({'found' if env_file.is_file() else 'not found'})")
    say(f"Python executable: {py}")
    say(f"Cloudflared executable: {cloudflared}")
    keys = sorted(k for k in env if k.startswith(CONFIG_PREFIXES) or k in CONFIG_EXTRAS)
    # Also show important absent settings to aid debugging.
    keys = sorted(set(keys) | {
        "AOB_EVENTS_URL", "AOB_OTEL_COMMIT", "AOB_OTEL_ENABLED",
        "AOB_OTEL_EXPORTER", "AOB_REPO_BRANCH", "AOB_KAGGLE_NOTEBOOK_REF",
        "KAGGLE_USERNAME", "KAGGLE_KEY", "AUTH0_DOMAIN",
        "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET",
    })
    for key in keys:
        say(f"  {key:<33} {_safe_config_value(key, env.get(key, ''))}")
    say("AOB_EVENTS_URL is generated after Cloudflare Tunnel starts; its startup value may be blank.")
    say("Sensitive and unrecognized values are redacted; do not print raw environment variables.")
    say("=========================================================")


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def await_port(port: int, proc: subprocess.Popen, deadline: float = 25.0) -> None:
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if proc.poll() is not None:
            raise RuntimeError(f"Process exited before 127.0.0.1:{port} was ready (exit={proc.returncode})")
        if port_open("127.0.0.1", port):
            return
        time.sleep(0.25)
    raise TimeoutError(f"127.0.0.1:{port} not listening after {deadline:.0f}s")


def run_git(repo: Path, *args: str) -> str | None:
    try:
        cp = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                            timeout=15, check=True)
        return cp.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_commit(repo: Path, env: dict[str, str]) -> None:
    if env.get("AOB_OTEL_ENABLED", "").lower() != "true":
        return
    if env.get("AOB_OTEL_COMMIT"):
        return
    branch = env["AOB_REPO_BRANCH"]
    remote = run_git(repo, "ls-remote", "origin", f"refs/heads/{branch}")
    sha = remote.split()[0] if remote else None
    if not sha:
        sha = run_git(repo, "rev-parse", f"refs/remotes/origin/{branch}")
        if sha:
            say("Warning: using locally cached origin branch SHA (remote not verified)")
    if not sha or not re.fullmatch(r"[a-fA-F0-9]{40}", sha):
        raise RuntimeError("Tracing requires AOB_OTEL_COMMIT. Set it in your environment or .env.local ")
    env["AOB_OTEL_COMMIT"] = sha
    say(f"Kaggle repository pinned to {branch} @ {sha[:12]} (confirm latest changes were pushed)")


class Orchestrator:
    def __init__(self, repo: Path, python: str, cloudflared: str, env: dict[str, str]):
        self.repo, self.python, self.cloudflared, self.env = repo, python, cloudflared, env
        self.processes: list[tuple[str, subprocess.Popen]] = []
        self.tunnel_found = threading.Event()
        self.tunnel_url: str | None = None
        self.stopping = False
        self._lock = threading.Lock()

    def start_process(self, name: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(cmd, cwd=self.repo, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
                                creationflags=flags)
        self.processes.append((name, proc))
        threading.Thread(target=self._watch, args=(name, proc), daemon=True).start()
        say(f"Started {name} (PID {proc.pid})")
        return proc

    def _watch(self, name: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if name == "TUNNEL":
                match = TUNNEL_RE.search(line)
                if match:
                    with self._lock:
                        self.tunnel_url = match.group(0).rstrip("/")
                        self.tunnel_found.set()
            # Service logs can include request metadata: avoid posting them publicly.
            print(f"[{name}] {line}", flush=True)

    def start(self, tunnel_wait: float) -> None:
        for port in (8011, 8010, 8501):
            if port_open("127.0.0.1", port):
                raise RuntimeError(f"Port {port} is already occupied; close old AOB services first")
        ingress = self.start_process("INGRESS", [self.python, "-m", "uvicorn", "api.public_ingress:app",
                                                 "--host", "127.0.0.1", "--port", "8011"], self.env.copy())
        await_port(8011, ingress)
        say("Event Ingress ready: http://127.0.0.1:8011")

        tunnel = self.start_process("TUNNEL", [self.cloudflared, "tunnel", "--url", "http://127.0.0.1:8011"],
                                    self.env.copy())
        until = time.monotonic() + tunnel_wait
        while not self.tunnel_found.wait(timeout=0.25):
            if tunnel.poll() is not None:
                raise RuntimeError(f"cloudflared exited (code {tunnel.returncode}); check TUNNEL logs")
            if time.monotonic() >= until:
                raise TimeoutError("Cloudflare Quick Tunnel URL not detected; check network/cloudflared logs")
        assert self.tunnel_url
        public_url = f"{self.tunnel_url}{DEFAULT_PATH}"
        control_env = self.env.copy()
        control_env["AOB_EVENTS_URL"] = public_url
        say(f"Current event callback: {public_url}")

        control = self.start_process("CONTROL", [self.python, "-m", "uvicorn", "api.private_app:app",
                                                 "--host", "127.0.0.1", "--port", "8010"], control_env)
        await_port(8010, control)
        say("Control API ready: http://127.0.0.1:8010")

        ui_env = control_env.copy()
        ui_env["AOB_API_URL"] = "http://127.0.0.1:8010"
        dashboard = self.start_process("STREAMLIT", [self.python, "-m", "streamlit", "run", "dashboard/app.py",
                                                  "--server.address", "127.0.0.1", "--server.port", "8501",
                                                  "--server.headless", "true"], ui_env)
        await_port(8501, dashboard, deadline=35)
        say("Dashboard: http://127.0.0.1:8501")
        say("Ready. Open dashboard and log in to start Live Demo. Press Ctrl+C to stop all services.")
        while True:
            for name, proc in self.processes:
                if proc.poll() is not None:
                    raise RuntimeError(f"{name} unexpectedly stopped (exit={proc.returncode})")
            time.sleep(1)

    def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        say("Stopping local services...")
        for name, proc in reversed(self.processes):
            if proc.poll() is not None:
                continue
            try:
                if os.name == "nt":
                    # Windows child processes can outlive their parent; kill the complete tree.
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except (OSError, subprocess.SubprocessError):
                proc.kill()
        say("Stopped. Note: an already-launched remote Kaggle run is not automatically cancelled.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Launch the 4 local AOB services with one command")
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent,
                    help="AOB repository root (default: directory holding this script)")
    ap.add_argument("--env-file", type=Path, default=None,
                    help="Optional private KEY=VALUE file (default: <repo>/.env.local)")
    ap.add_argument("--cloudflared", default=None, help="Path to cloudflared.exe (otherwise PATH)")
    ap.add_argument("--python", default=None, help="Python executable (otherwise repo .venv, then current Python)")
    ap.add_argument("--tunnel-timeout", type=float, default=75, help="Wait for Quick Tunnel URL (seconds)")
    args = ap.parse_args()
    repo = args.repo.expanduser().resolve()
    if not (repo / "api" / "private_app.py").is_file() or not (repo / "dashboard" / "app.py").is_file():
        say("ERROR: --repo must point to the independent agent-ops-board source root")
        return 2
    env = os.environ.copy()
    load_env_file(args.env_file or repo / ".env.local", env)
    env["PYTHONPATH"] = str(repo) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("AOB_KAGGLE_NOTEBOOK_REF", DEFAULT_REF)
    env.setdefault("AOB_REPO_BRANCH", "OPENTELE")
    env.setdefault("AOB_OTEL_ENABLED", "true")
    env.setdefault("AOB_OTEL_EXPORTER", "console")
    env.setdefault("AOB_API_URL", "http://127.0.0.1:8010")

    venv = repo / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    py = args.python or (str(venv) if venv.is_file() else sys.executable)
    cf = args.cloudflared or shutil.which("cloudflared")
    if not cf:
        say("ERROR: cloudflared not found. Install it or pass --cloudflared C:\\path\\cloudflared.exe")
        return 2
    try:
        resolve_commit(repo, env)
    except Exception as exc:
        say(f"ERROR: {exc}")
        return 2
    if env["AOB_OTEL_ENABLED"].lower() == "true":
        say("Tracing enabled; commit " + env["AOB_OTEL_COMMIT"][:12])
    say("Secrets are inherited from environment/.env.local; not embedded in this script")
    print_effective_config(env, args.env_file or repo / ".env.local", py, cf)
    orchestrator = Orchestrator(repo, py, cf, env)
    try:
        orchestrator.start(args.tunnel_timeout)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        say(f"ERROR: {type(exc).__name__}: {exc}")
        return_code = 1
    else:
        return_code = 0
    finally:
        orchestrator.stop()
    return locals().get("return_code", 0)


if __name__ == "__main__":
    raise SystemExit(main())
