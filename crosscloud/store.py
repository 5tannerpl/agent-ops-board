"""Private schema for local metadata. Legacy and public-ingress connections do not migrate it."""
import hashlib
import json
import sqlite3
from uuid import uuid4

import config
from storage import db
from .policy import ServiceError


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS deployments (
 deployment_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, provider TEXT NOT NULL,
 runtime_type TEXT NOT NULL, ownership TEXT NOT NULL DEFAULT 'external',
 connection_mode TEXT NOT NULL, execution_policy TEXT NOT NULL DEFAULT 'observe_only',
 endpoint_ref TEXT, credential_ref TEXT, source_id TEXT, image_ref TEXT, image_digest TEXT,
 provider_resource_id TEXT, connector_ref TEXT, telemetry_policy_ref TEXT,
 capabilities TEXT NOT NULL, config_version INTEGER NOT NULL DEFAULT 1,
 status TEXT NOT NULL DEFAULT 'registered', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deployments_owner ON deployments(owner_id,created_at,deployment_id);
CREATE TABLE IF NOT EXISTS deployment_bindings (
 run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, source_id TEXT NOT NULL,
 remote_run_id TEXT NOT NULL, agent_deployment_id TEXT NOT NULL, agent_version INTEGER NOT NULL,
 inference_deployment_id TEXT, inference_version INTEGER, workload_version TEXT NOT NULL,
 deadline REAL NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
 business_status TEXT NOT NULL DEFAULT 'unknown', telemetry_status TEXT NOT NULL DEFAULT 'unknown',
 created_at REAL NOT NULL, UNIQUE(owner_id,source_id,remote_run_id)
);
CREATE INDEX IF NOT EXISTS ix_bindings_owner ON deployment_bindings(owner_id,source_id,created_at);
-- Reserved for outbound starts. Observed external runs have no dispatch and are never queued.
CREATE TABLE IF NOT EXISTS run_dispatch (
 run_id TEXT PRIMARY KEY REFERENCES deployment_bindings(run_id), state TEXT NOT NULL DEFAULT 'reserved',
 attempts INTEGER NOT NULL DEFAULT 0, last_error_code TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
 operation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, operation_type TEXT NOT NULL,
 key_hash TEXT NOT NULL, request_hash TEXT NOT NULL, result TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(owner_id,operation_type,key_hash)
);
COMMIT;
"""


def reader():
    conn = sqlite3.connect(config.DEMO_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def writer():
    return db.connect(config.DEMO_DB_PATH)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def record_once(conn, principal, operation, key, payload, action):
    """Reserve the writer slot before looking up the key. Local effect and receipt commit together."""
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise ServiceError(422, "INVALID_IDEMPOTENCY_KEY")
    key_hash = digest(key)
    request_hash = digest(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))
    with db.tx(conn):
        identity = principal.owner, operation, key_hash
        row = conn.execute("SELECT request_hash,result FROM operations WHERE owner_id=? "
                           "AND operation_type=? AND key_hash=?", identity).fetchone()
        if row:
            if row["request_hash"] != request_hash:
                raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
            return json.loads(row["result"])
        result = action()
        conn.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                     (uuid4().hex, *identity, request_hash, json.dumps(result), db.now()))
        return result
