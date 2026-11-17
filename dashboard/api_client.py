"""Thin HTTP client for the Agent Ops Board API (the UI never touches SQLite or the model)."""
import os

import httpx

API_URL = os.environ.get("AOB_API_URL", "http://127.0.0.1:8000")


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, **kwargs):
    try:
        resp = httpx.request(method, API_URL + path, timeout=10, **kwargs)
    except httpx.HTTPError as exc:
        raise ApiError(f"API unreachable at {API_URL} ({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise ApiError(str(detail)[:300], resp.status_code)
    return resp.json()


def dashboard(test_run_id: str | None = None) -> dict:
    return _request("GET", "/v1/dashboard", params={"test_run_id": test_run_id} if test_run_id else None)


def workers() -> list[dict]:
    return _request("GET", "/v1/workers")["items"]


def worker(worker_id: str) -> dict:
    return _request("GET", f"/v1/workers/{worker_id}")


def worker_events(worker_id: str, limit: int = 100) -> list[dict]:
    return _request("GET", f"/v1/workers/{worker_id}/events", params={"limit": limit})["items"]


def intake_units(**params) -> dict:
    return _request("GET", "/v1/intake-units", params={k: v for k, v in params.items() if v not in (None, "")})


def intake_unit(unit_id: str) -> dict:
    return _request("GET", f"/v1/intake-units/{unit_id}")


def intake_steps(unit_id: str) -> dict:
    return _request("GET", f"/v1/intake-units/{unit_id}/steps")


def intake_handoff(unit_id: str) -> dict:
    return _request("GET", f"/v1/intake-units/{unit_id}/handoff")["handoff"]


def start_load_test(**body) -> dict:
    return _request("POST", "/v1/load-tests", json=body)


def stop_load_test(run_id: str) -> dict:
    return _request("POST", f"/v1/load-tests/{run_id}/stop")


def submit_message(**body) -> dict:
    return _request("POST", "/v1/messages", json=body)


# ---- Private live demo (Auth0 access token required) ----------------------------------------------
def _bearer(token: str) -> dict:
    return {"headers": {"Authorization": f"Bearer {token}"}}


def me(token: str) -> dict:
    return _request("GET", "/api/me", **_bearer(token))


def demo_start(token: str, mode: str = "light", units: int | None = None) -> dict:
    body = {"mode": mode, **({"units": units} if units else {})}
    return _request("POST", "/api/demo/start", json=body, **_bearer(token))


def demo_runs(token: str) -> list[dict]:
    return _request("GET", "/api/demo/runs", **_bearer(token))["items"]


def demo_status(token: str, run_id: str) -> dict:
    return _request("GET", f"/api/demo/{run_id}/status", **_bearer(token))


def demo_validation(token: str, run_id: str) -> list[dict]:
    return _request("GET", f"/api/demo/{run_id}/validation", **_bearer(token))["items"]


def demo_state(token: str, run_id: str) -> dict:
    return _request("GET", f"/api/demo/{run_id}/state", **_bearer(token))


def demo_metrics(token: str, run_id: str) -> list[dict]:
    return _request("GET", f"/api/demo/{run_id}/metrics", **_bearer(token))["points"]
