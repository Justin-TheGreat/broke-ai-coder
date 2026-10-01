from __future__ import annotations

import logging
import sqlite3

import pytest

from app.db.connection import transaction


def test_commit_and_rollback(db):
    db.execute("CREATE TABLE t (x INTEGER)")
    with transaction(db):
        db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(KeyError):
        with transaction(db):
            db.execute("INSERT INTO t VALUES (2)")
            raise KeyError("boom")
    assert [r[0] for r in db.execute("SELECT x FROM t")] == [1]


class _Stub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, sql: str):
        self.calls.append(sql)
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("cannot rollback - no transaction is active")


def test_failed_rollback_preserves_original_exception(caplog):
    stub = _Stub()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyError):
            with transaction(stub):  # type: ignore[arg-type]
                raise KeyError("original")
    assert stub.calls == ["BEGIN IMMEDIATE", "ROLLBACK"]
    assert "rollback failed: OperationalError" in caplog.text
