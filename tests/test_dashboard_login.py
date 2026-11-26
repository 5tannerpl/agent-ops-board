from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import api_client

PAGE = str(Path(__file__).resolve().parents[1] / "dashboard" / "pages" / "worker_monitor.py")


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("AOB_REQUIRE_LOGIN", "1")
    monkeypatch.setattr(api_client, "workers", lambda: [])

    def make(user, me=lambda token: {"user_id": "auth0|alice"}):
        monkeypatch.setattr(api_client, "me", me)
        at = AppTest.from_file(PAGE, default_timeout=20)
        at.user = user
        return at.run()
    return make


def test_login_gate_blocks_anonymous_users(gated):
    at = gated({"is_logged_in": False})
    assert [b.label for b in at.button] == ["Log in"]
    assert "invited users only" in at.info[0].value  # page body not rendered


def test_login_gate_blocks_non_whitelisted_user(gated):
    def denied(token):
        raise api_client.ApiError("Demo access denied", 403)
    at = gated({"is_logged_in": True, "tokens": {"access": "t"}}, me=denied)
    assert "whitelist" in at.error[0].value


def test_login_gate_requires_an_access_token(gated):
    at = gated({"is_logged_in": True, "tokens": {}})
    assert "access token" in at.error[0].value


def test_whitelisted_user_sees_the_page(gated):
    at = gated({"is_logged_in": True, "tokens": {"access": "t"}, "name": "Alice"})
    assert not at.error
    assert "No workers registered yet." in [i.value for i in at.info]


def test_selection_survives_observability_navigation_and_revocation(monkeypatch):
    rows = [dict(run_id=k, provider="local", agent_deployment_id="agent", source_id="source",
                 business_status="running", workload_version="v1") for k in ("recent", "older")]
    monkeypatch.setattr(api_client, "bindings", lambda token, **params: {
        "items": [r for r in rows if params.get("search", "") in r["run_id"]]})
    reads = []
    def observation(token, local, section):
        reads.append((token, local, section))
        return {"span_count": 0}
    monkeypatch.setattr(api_client, "observation", observation)
    def metrics(token, local):
        reads.append((token, local, "metrics"))
        return {"observed_calls": None, "usage": None}
    monkeypatch.setattr(api_client, "bound_llm_metrics", metrics)
    at = AppTest.from_string("""
import streamlit as st
import workspace
view = st.radio('View', ['Agent', 'Infrastructure', 'LLM'])
{'Agent': workspace.agent, 'LLM': workspace.llm, 'Infrastructure': workspace.infrastructure}[view]('token', {})
""").run()
    at.selectbox[0].select("older").run()
    for view in ("Infrastructure", "LLM"):
        at.radio[0].set_value(view).run()
        assert not at.exception and at.selectbox[0].value == "older"
    assert reads[-1] == ("token", "older", "metrics")
    rows.pop()
    at.run()
    assert not at.exception and reads[-1] == ("token", "recent", "metrics")
    assert any("no longer available" in warning.value for warning in at.warning)


def test_azure_panel_reuses_lost_receipt_key_then_only_reads(monkeypatch):
    import config
    calls = []
    def private(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if len(calls) == 1:
            raise api_client.ApiError("receipt lost")
        if method == "POST":
            return {"run_id": "local"}
        return {"dispatch": {"state": "uncertain"}, "business_status": "unknown",
                "telemetry_status": "unknown", "remote_run_id": "remote"}

    monkeypatch.setattr(api_client, "_request", private)
    monkeypatch.setattr(config, "CROSS_CLOUD_ENABLED", True)
    monkeypatch.setenv("AOB_AZURE_RUN_DEPLOYMENT_ID", "approved")
    at = AppTest.from_string('import azure_run; azure_run.render("user-token")').run()
    assert not at.button and not calls and not at.exception
    monkeypatch.setattr(config, "REMOTE_RUNS_ENABLED", True)
    at.run().button[0].click().run()
    at.button[0].click().run()
    at.run()
    assert not at.exception and not at.button and at.session_state["azure_local_run"] == "local"
    assert at.session_state["selected_run"] == "local"
    assert calls[0] == calls[1] and calls[0][:2] == ("POST", "/api/v1/runs")
    assert calls[0][2]["json"]["agent_deployment_id"] == "approved"
    assert calls[0][2]["headers"]["Idempotency-Key"]
    assert calls[2:] and all(c[:2] == ("GET", "/api/v1/runs/local") for c in calls[2:])
