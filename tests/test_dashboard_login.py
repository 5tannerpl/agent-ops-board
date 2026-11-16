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
