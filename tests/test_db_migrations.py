from __future__ import annotations

import pytest

from app.db.connection import db_size_bytes, open_database, transaction
from app.db.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    SchemaTooNewError,
    current_version,
    migrate,
)

TABLES = {
    "projects",
    "sessions",
    "tasks",
    "provider_credentials_metadata",
    "provider_models",
    "provider_model_policy",
    "quota_snapshots",
    "provider_events",
    "approvals",
    "usage_events",
    "fallback_attempts",
    "schema_version",
}


def test_fresh_reaches_latest(db):
    assert current_version(db) == LATEST_VERSION
    assert [m.version for m in MIGRATIONS] == list(range(1, LATEST_VERSION + 1))


def test_current_version_zero_before_migrate(tmp_path):
    conn = open_database(tmp_path / "x.db")
    try:
        assert current_version(conn) == 0
    finally:
        conn.close()


def test_pragmas(tmp_path):
    conn = open_database(tmp_path / "x.db", busy_timeout_ms=1234)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_creates_parent_dirs(tmp_path):
    conn = open_database(tmp_path / "a" / "b" / "x.db")
    conn.close()
    assert (tmp_path / "a" / "b" / "x.db").exists()


def test_tables_exist(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert TABLES <= names


def test_migrate_idempotent(db):
    assert migrate(db) == LATEST_VERSION
    assert migrate(db) == LATEST_VERSION
    rows = db.execute("SELECT version FROM schema_version").fetchall()
    assert len(rows) == LATEST_VERSION


def test_schema_too_new(db):
    db.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (?, 'x')",
        (LATEST_VERSION + 1,),
    )
    with pytest.raises(SchemaTooNewError):
        migrate(db)


def test_no_raw_key_columns(db):
    for t in TABLES:
        cols = {r[1].lower() for r in db.execute(f"PRAGMA table_info({t})")}
        assert not any("api_key" in c and c != "api_key_env" for c in cols), t


def test_transaction_rollback_and_commit(db):
    with transaction(db):
        db.execute("INSERT INTO projects VALUES ('p','n','/w',NULL,NULL,'t','t')")
    with pytest.raises(RuntimeError):
        with transaction(db):
            db.execute("INSERT INTO projects VALUES ('q','n2','/w',NULL,NULL,'t','t')")
            raise RuntimeError("boom")
    assert [r[0] for r in db.execute("SELECT id FROM projects")] == ["p"]
    assert not db.in_transaction


def test_db_size(db):
    assert db_size_bytes(db) > 0
