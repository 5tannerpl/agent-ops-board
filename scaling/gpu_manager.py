"""llama-server instance lifecycle (STOPPED -> STARTING -> READY -> DRAINING -> STOPPED / FAILED) and
best-effort NVIDIA telemetry. Readiness always comes from the instance's own /health, never from a GPU number."""
import os
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

STOPPED, STARTING, READY, DRAINING, FAILED = "STOPPED", "STARTING", "READY", "DRAINING", "FAILED"


def _smi(args: list[str]) -> list[list[str]]:
    try:
        out = subprocess.run(["nvidia-smi", *args, "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [[c.strip() for c in line.split(",")] for line in out.stdout.splitlines() if line.strip()]


def _num(value: str) -> int | None:
    try:
        return int(float(value))
    except ValueError:
        return None


def gpu_stats() -> dict[int, dict]:
    """{gpu_index: {memory_used_mb, memory_total_mb, utilization_pct}}; empty when nvidia-smi is unavailable."""
    stats = {}
    for row in _smi(["--query-gpu=index,memory.used,memory.total,utilization.gpu"]):
        if len(row) == 4 and _num(row[0]) is not None:
            stats[_num(row[0])] = {"memory_used_mb": _num(row[1]), "memory_total_mb": _num(row[2]),
                                   "utilization_pct": _num(row[3])}
    return stats


def gpu_index_of_pid(pid: int) -> int | None:
    """GPU index the PID holds a CUDA context on; None when nvidia-smi cannot attribute it (e.g. hidden PIDs)."""
    uuids = {row[1]: _num(row[0]) for row in _smi(["--query-gpu=index,uuid"]) if len(row) == 2}
    for row in _smi(["--query-compute-apps=pid,gpu_uuid"]):
        if len(row) == 2 and _num(row[0]) == pid:
            return uuids.get(row[1])
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def supports_device_flag(llama_server: str, env: dict | None = None) -> bool:
    """The saved CUDA binary may predate --device; never assume, read its --help."""
    try:
        out = subprocess.run([llama_server, "--help"], capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--device" in (out.stdout + out.stderr)


class LlamaInstance:
    """One llama-server process pinned to one GPU."""

    def __init__(self, name: str, gpu_id: int, port: int, llama_server: str | Path, model_path: str | Path,
                 model_alias: str, log_path: str | Path, env: dict | None = None, ctx: int = 4096,
                 startup_timeout: float = 90.0):
        self.name, self.gpu_id, self.port = name, gpu_id, port
        self.llama_server, self.model_path, self.model_alias = str(llama_server), str(model_path), model_alias
        self.log_path, self.env, self.ctx, self.startup_timeout = Path(log_path), dict(env or os.environ), ctx, startup_timeout
        self.state = STOPPED
        self.owned = True  # False when adopting a server another notebook cell started
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None
        self.exit_code: int | None = None
        self.error: str | None = None
        self.load_seconds: float | None = None
        self.pinned_via: str | None = None
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def healthy(self) -> bool:
        return http_ok(self.url + "/health")

    
    def command_and_env(self) -> tuple[list[str], dict]:
        cmd = [
            self.llama_server,
            "-m", self.model_path,
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "-ngl", "99",
            "-c", str(self.ctx),
            "--alias", self.model_alias,
        ]

        env = dict(self.env)

        # Expose only the intended physical GPU to this process.
        env["CUDA_VISIBLE_DEVICES"] = str(self.gpu_id)

        if supports_device_flag(self.llama_server, env):
            # Within the restricted CUDA namespace, the
            # selected GPU is always logical CUDA0.
            cmd += [
                "--device", "CUDA0",
                "--split-mode", "none",
            ]
            self.pinned_via = (
                f"CUDA_VISIBLE_DEVICES={self.gpu_id}, "
                "--device CUDA0"
            )
        else:
            self.pinned_via = (
                f"CUDA_VISIBLE_DEVICES={self.gpu_id}"
            )

        return cmd, env


    def adopt(self, pid: int | None = None) -> None:
        self.owned, self.pid, self.state, self.pinned_via = False, pid, READY, "adopted"

    def start(self) -> bool:
        """Blocking: spawn and wait for /health. Returns True only when READY."""
        with self._lock:
            if self.state in (STARTING, READY):
                return self.state == READY
            self.state, self.error, self.exit_code = STARTING, None, None
        started = time.monotonic()
        try:
            if port_open(self.port):
                raise RuntimeError(f"port {self.port} is already in use")
            cmd, env = self.command_and_env()
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "w") as log:
                self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
            self.pid = self.proc.pid
            deadline = started + self.startup_timeout
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"llama-server exited with code {self.proc.returncode}")
                if self.healthy():
                    self.load_seconds = round(time.monotonic() - started, 2)
                    self.state = READY
                    return True
                time.sleep(1)
            raise RuntimeError(f"/health not ready within {self.startup_timeout:.0f}s")
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            self.stop()
            self.state = FAILED
            return False

    def mark_draining(self) -> None:
        if self.state == READY:
            self.state = DRAINING

    def stop(self, timeout: float = 20.0) -> int | None:
        """Terminate (then kill) an owned process; returns its exit code. Adopted servers are left alone."""
        if self.owned and self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(10)
            self.exit_code = self.proc.returncode
        if self.owned and self.state != FAILED:
            self.state = STOPPED
        return self.exit_code

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def error_log_tail(self, chars: int = 300) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-chars:]
        except OSError:
            return ""
