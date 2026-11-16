"""Stand-in for llama-server used by tests: same CLI flags, /health, /props and /v1/chat/completions.

Answers come from the rule-based MockClient, so this exercises routing and scaling, never model quality.
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workers.model_client import MockClient  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--device")
    args, _ = parser.parse_known_args()
    latency = float(os.environ.get("FAKE_LLAMA_LATENCY", "0.3"))
    if args.device and args.device == os.environ.get("FAKE_LLAMA_FAIL_DEVICE"):
        sys.exit(3)  # simulates a device that cannot load the model
    mock = MockClient()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._send(200, {"status": "ok"} if self.path == "/health" else {"build_info": "fake"})

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            time.sleep(latency)
            user = body["messages"][-1]["content"]
            self._send(200, {"choices": [{"message": {"content": mock.generate("", user, 1)}}]})

        def log_message(self, *_):
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64

    Server(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    if "--help" in sys.argv:
        print("--device <dev1,dev2,..>  --split-mode {none,layer,row}")
        sys.exit(0)
    main()
