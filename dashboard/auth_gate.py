"""Auth0 login gate for the Streamlit UI (Universal Login via ``st.login``).

The gate is active when ``[auth]`` is present in ``.streamlit/secrets.toml`` or ``AOB_REQUIRE_LOGIN=1``;
otherwise the UI behaves as before (local development). The UI check only decides what to render: the
FastAPI service verifies the access token and the whitelist again on every call.
"""
import os

import streamlit as st

import api_client
import config

PROVIDER = os.environ.get("AOB_AUTH_PROVIDER", "auth0")


def login_required() -> bool:
    if config.AOB_ENV == "production":
        return True
    if os.environ.get("AOB_REQUIRE_LOGIN", "").lower() in ("1", "true", "yes"):
        return True
    try:
        return "auth" in st.secrets
    except Exception:  # no secrets file
        return False


def access_token() -> str | None:
    try:
        return st.user.tokens["access"]
    except (KeyError, AttributeError, TypeError):
        return None


def _stop_with_logout(kind: str, message: str):
    getattr(st, kind)(message)
    if st.button("Log out"):
        st.logout()
    st.stop()


def require_login() -> str | None:
    """Return the API access token for a whitelisted user, ``None`` when login is disabled, else stop."""
    if not login_required():
        return None
    if config.AOB_ENV == "production":
        try:
            has_auth = "auth" in st.secrets
        except Exception:
            has_auth = False
        if not has_auth:
            _stop_with_logout("error", "Authentication is required in production, but auth config is missing.")
    if not st.user.is_logged_in:
        st.info("Private demo: invited users only. There is no public sign-up.")
        if st.button("Log in", type="primary"):
            st.login(PROVIDER)
        st.stop()
    token = access_token()
    if not token:
        _stop_with_logout("error", "No API access token was issued. Check `expose_tokens` and the Auth0 "
                                   "`audience` in `.streamlit/secrets.toml`.")
    try:
        api_client.me(token)
    except api_client.ApiError as exc:
        if exc.status == 403:
            _stop_with_logout("error", "This account is not on the demo whitelist.")
        if exc.status == 401:
            _stop_with_logout("warning", "Your session expired or the token is invalid. Log out and sign in again.")
        _stop_with_logout("error", f"Account verification unavailable ({exc.status or 'connection error'}).")
    with st.sidebar:
        st.caption(f"Signed in as {st.user.get('name') or st.user.get('email') or 'user'}")
        if st.button("Log out"):
            st.logout()
    return token
