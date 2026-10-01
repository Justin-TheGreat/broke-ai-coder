from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def open_database(
    path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000
) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"could not enable WAL journal mode (got {mode!r})")
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
    except BaseException:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def db_size_bytes(conn: sqlite3.Connection) -> int:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    return int(pages) * int(size)
