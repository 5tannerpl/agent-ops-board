"""Minimal OpenAI-compatible reverse proxy in front of the llama.cpp instances.

Workers keep a single ``AOB_MODEL_URL``; the router sends each request to the READY backend with the fewest
in-flight requests and keeps per-backend counters, which are the evidence that a GPU served inference.
A request is never retried here: the worker owns retry policy.
"""
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

READY, DRAINING = "READY", "DRAINING"
_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}


class Backend:
    def __init__(self, name: str, url: str):
        self.name, self.url, self.state = name, url.rstrip("/"), READY
        self.inflight = self.started = self.completed = self.failed = 0
        self._unit_gpu_map: dict[str, str] = {}

    def view(self) -> dict:
        return {"state": self.state, "inflight": self.inflight, "requests": self.started,
                "completed": self.completed, "failed": self.failed}


class InferenceRouter:
    def __init__(self, host: str = "127.0.0.1", port: int = 8090, upstream_timeout: float = 180.0):
        self.host, self.port, self.upstream_timeout = host, port, upstream_timeout
        self._backends: dict[str, Backend] = {}
        self._history: dict[str, dict] = {}  # counters survive removal so final evidence is kept
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None

    # ---- membership ------------------------------------------------------
    def add_backend(self, name: str, url: str) -> None:
        with self._lock:
            backend = Backend(name, url)
            prior = self._history.pop(name, None)
            if prior:  # a restarted backend keeps its cumulative counters
                backend.started, backend.completed, backend.failed = (
                    prior["requests"], prior["completed"], prior["failed"])
            self._backends[name] = backend

    def drain(self, name: str) -> None:
        """Stop sending new requests; in-flight ones finish."""
        with self._lock:
            if name in self._backends:
                self._backends[name].state = DRAINING

    def remove(self, name: str) -> None:
        with self._lock:
            backend = self._backends.pop(name, None)
            if backend:
                self._history[name] = backend.view()

    def active_backends(self) -> list[str]:
        with self._lock:
            return sorted(n for n, b in self._backends.items() if b.state == READY)

    def inflight(self, name: str) -> int:
        with self._lock:
            return self._backends[name].inflight if name in self._backends else 0

    def stats(self) -> dict:
        with self._lock:
            out = {n: dict(v) for n, v in self._history.items()}
            out.update({n: b.view() for n, b in self._backends.items()})
        return out
    
    def unit_attribution(self, completed_unit_ids) -> dict:
        """
        Count unique completed units by their last successful
        inference backend.
        """
        ids = set(completed_unit_ids)

        with self._lock:
            attribution = {
                uid: gpu
                for uid, gpu in self._unit_gpu_map.items()
                if uid in ids
            }

        gpu0 = sum(1 for gpu in attribution.values() if gpu == "gpu0")
        gpu1 = sum(1 for gpu in attribution.values() if gpu == "gpu1")

        return {
            "gpu0": gpu0,
            "gpu1": gpu1,
            "unattributed": len(ids) - len(attribution),
            "total": len(ids),
        }

    def _record_unit_success(self, unit_id: str | None, gpu: str):
        if not unit_id:
            return

        with self._lock:
            self._unit_gpu_map[unit_id] = gpu

    # ---- proxying --------------------------------------------------------
    def _acquire(self) -> Backend | None:
        with self._lock:
            ready = [b for b in self._backends.values() if b.state == READY]
            if not ready:
                return None
            backend = min(ready, key=lambda b: (b.inflight, b.started))
            backend.inflight += 1
            backend.started += 1
            return backend

    def _release(self, backend: Backend, ok: bool) -> None:
        with self._lock:
            backend.inflight -= 1
            if ok:
                backend.completed += 1
            else:
                backend.failed += 1

    def forward(
        self,
        method: str,
        path: str,
        body: bytes | None,
        headers: dict,
        unit_id: str | None = None,
    ) -> tuple[int, dict, bytes]:
        backend = self._acquire()
        if backend is None:
            return 503, {"Content-Type": "application/json"}, json.dumps(
                {"error": {"message": "no inference backend is ready", "type": "unavailable"}}).encode()
        ok = False
        try:
            target = urlsplit(backend.url)
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=self.upstream_timeout)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
                ok = resp.status < 500
                if 200 <= resp.status < 300:
                    self._record_unit_success(unit_id, backend.name)
                out_headers = {k: v for k, v in resp.getheaders() if k.lower() not in _HOP}
                return resp.status, out_headers, data
            finally:
                conn.close()
        except TimeoutError:
            return 504, {"Content-Type": "application/json"}, json.dumps(
                {"error": {"message": "backend timeout", "type": "timeout"}}).encode()
        except OSError:
            return 502, {"Content-Type": "application/json"}, json.dumps(
                {"error": {"message": "backend unreachable", "type": "bad_gateway"}}).encode()
        finally:
            self._release(backend, ok)
    
    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        router = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status: int, headers: dict, data: bytes) -> None:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _json(self, status: int, obj: dict) -> None:
                self._send(status, {"Content-Type": "application/json"}, json.dumps(obj).encode())

            def _proxy(self, method: str) -> None:
                if self.path == "/router/stats":
                    return self._json(200, router.stats())
                if self.path == "/health" and method == "GET":
                    ready = router.active_backends()
                    return self._json(200 if ready else 503, {"status": "ok" if ready else "unavailable",
                                                              "backends": ready})
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                fwd = {
                    "Content-Type": self.headers.get(
                        "Content-Type",
                        "application/json"
                    )
                }

                unit_id = self.headers.get("X-AOB-Unit-ID")

                self._send(*router.forward(
                    method,
                    self.path,
                    body,
                    fwd,
                    unit_id=unit_id,
                ))

            def do_GET(self):  # noqa: N802
                self._proxy("GET")

            def do_POST(self):  # noqa: N802
                self._proxy("POST")

            def log_message(self, *_):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 128
            allow_reuse_address = True

        self._server = Server((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, name="inference-router", daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
