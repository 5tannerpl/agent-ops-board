"""Start workers, API and dashboard together (cross-platform): python run_demo.py [--workers 2]"""
import argparse
import subprocess
import sys
import time

import config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8501)
    args = parser.parse_args()
    py = sys.executable
    cmds = {
        "workers": [py, "-m", "workers.supervisor", "--workers", str(args.workers)],
        "api": [py, "-m", "uvicorn", "api.main:app", "--port", str(args.api_port)],
        "ui": [py, "-m", "streamlit", "run", "dashboard/app.py", "--server.port", str(args.ui_port)],
    }
    env = None
    if args.api_port != 8000:
        import os
        env = {**os.environ, "AOB_API_URL": f"http://127.0.0.1:{args.api_port}"}
    procs = {name: subprocess.Popen(cmd, cwd=config.ROOT, env=env) for name, cmd in cmds.items()}
    print(f"model backend={config.MODEL_BACKEND} name={config.MODEL_NAME}; db={config.DB_PATH}")
    print(f"UI: http://localhost:{args.ui_port}   API: http://127.0.0.1:{args.api_port}   Ctrl-C to stop")
    try:
        while all(p.poll() is None for p in procs.values()):
            time.sleep(1)
        dead = [n for n, p in procs.items() if p.poll() is not None]
        print(f"process exited: {dead}; stopping the rest", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
