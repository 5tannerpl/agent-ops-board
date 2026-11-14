import pytest

import config
from storage import db


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated runtime dir + DB; mock model; fast retry/lease timings."""
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "ATTACHMENT_DIR", tmp_path / "attachments")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "intake.db")
    monkeypatch.setattr(config, "MODEL_BACKEND", "mock")
    monkeypatch.setattr(config, "RETRY_DELAYS", [0.2, 0.2])
    monkeypatch.setattr(config, "LEASE_SECONDS", 1.5)
    monkeypatch.setattr(config, "HEARTBEAT_SECONDS", 0.3)
    monkeypatch.setattr(config, "POLL_SECONDS", 0.05)
    conn = db.connect()
    db.init_schema(conn)
    yield conn
    conn.close()
