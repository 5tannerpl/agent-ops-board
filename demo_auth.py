"""Auth0 access-token verification, user whitelist and Kaggle event signatures.

Login success is not authorisation: a valid Auth0 token is only accepted for demo endpoints when its
``sub`` is listed and enabled in the whitelist file, which is re-read on every request.
"""
import hashlib
import hmac
import json
import re
import time
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

import config

BEARER = HTTPBearer(auto_error=False)
SIGNATURE_RE = re.compile(r"^sha256=[0-9a-f]{64}$")


@lru_cache(maxsize=4)
def _jwks_client(domain: str) -> PyJWKClient:
    return PyJWKClient(f"https://{domain}/.well-known/jwks.json", timeout=10)


def signing_key(token: str):
    return _jwks_client(config.AUTH0_DOMAIN).get_signing_key_from_jwt(token).key


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)) -> dict:
    if not config.AUTH0_DOMAIN or not config.AUTH0_AUDIENCE:
        raise HTTPException(503, "Auth0 is not configured on the server")
    if credentials is None:
        raise _unauthorized("Access token required")
    try:
        return jwt.decode(
            credentials.credentials, signing_key(credentials.credentials), algorithms=["RS256"],
            audience=config.AUTH0_AUDIENCE, issuer=f"https://{config.AUTH0_DOMAIN}/", leeway=10,
            options={"require": ["exp", "iss", "sub", "aud"]})
    except jwt.PyJWTError:
        raise _unauthorized("Invalid access token")


def load_allowed_users() -> dict:
    """Fail closed: a missing or malformed whitelist allows nobody."""
    try:
        data = json.loads(config.ALLOWED_USERS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    users = data.get("allowed_users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def require_demo_access(user: dict = Depends(require_user)) -> dict:
    record = load_allowed_users().get(user["sub"])
    if not isinstance(record, dict) or record.get("enabled") is not True:
        raise HTTPException(403, "Demo access denied")
    return user


# ---- Kaggle -> /api/events request signing -----------------------------------------------------

def run_event_key(run_id: str, version: int = 1) -> str:
    """Per-run key: the notebook only ever holds a key that is valid for its own run_id."""
    return hmac.new(config.get_event_secret().encode(), f"run:{run_id}:v:{version}".encode(), hashlib.sha256).hexdigest()


def sign_event(run_id: str, timestamp: str, body: bytes, version: int = 1) -> str:
    mac = hmac.new(run_event_key(run_id, version).encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify_event(run_id: str, timestamp: str | None, signature: str | None, body: bytes, version: int = 1) -> bool:
    if not config.get_event_secret() or not timestamp or not signature or not SIGNATURE_RE.match(signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > config.EVENT_MAX_SKEW_SECONDS:
            return False
    except ValueError:
        return False
    return hmac.compare_digest(signature, sign_event(run_id, timestamp, body, version))
