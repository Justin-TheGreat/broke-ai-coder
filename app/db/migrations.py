from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.db.connection import transaction
from app.timeutil import to_db, utcnow


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    statements: tuple[str, ...]


_V1: tuple[str, ...] = (
    """CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  working_directory TEXT NOT NULL, git_remote TEXT, policy_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE sessions (id TEXT PRIMARY KEY, opencode_session_id TEXT,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('OPEN','CLOSED')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT)""",
    """CREATE TABLE tasks (id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
  session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
  discord_guild_id TEXT, discord_channel_id TEXT, discord_user_id TEXT NOT NULL,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('QUEUED','ROUTING','RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')),
  selected_provider TEXT, selected_model TEXT,
  cost_class TEXT CHECK (cost_class IS NULL OR cost_class IN ('FREE','PAID')),
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, last_event_at TEXT NOT NULL,
  exit_code INTEGER, error_class TEXT)""",
    "CREATE INDEX idx_tasks_status ON tasks(status)",
    "CREATE INDEX idx_tasks_finished_at ON tasks(finished_at)",
    "CREATE INDEX idx_tasks_session_id ON tasks(session_id)",
    """CREATE TABLE provider_credentials_metadata (provider TEXT PRIMARY KEY,
  api_key_env TEXT NOT NULL, present INTEGER NOT NULL CHECK (present IN (0,1)),
  last_checked_at TEXT, last_auth_failure_at TEXT)""",
    """CREATE TABLE provider_models (provider TEXT NOT NULL, model TEXT NOT NULL,
  routing_status TEXT NOT NULL CHECK (routing_status IN ('ALLOWED','DISCOVERED_ONLY')),
  supports_tool_calling INTEGER, supports_structured_output INTEGER, supports_vision INTEGER,
  context_window INTEGER, max_output_tokens INTEGER,
  discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (provider, model))""",
    """CREATE TABLE provider_model_policy (id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model TEXT NOT NULL,
  priority INTEGER NOT NULL, enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
  cost_class TEXT NOT NULL CHECK (cost_class IN ('FREE','PAID')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (provider, model))""",
    """CREATE TABLE quota_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model TEXT,
  quota_window TEXT NOT NULL, limit_value NUMERIC, used NUMERIC, remaining NUMERIC,
  unit TEXT NOT NULL CHECK (unit IN ('REQUESTS','TOKENS','USD')),
  confidence TEXT NOT NULL
    CHECK (confidence IN ('EXACT','ESTIMATED','UNKNOWN','EXHAUSTED','COOLDOWN')),
  reset_at TEXT, observed_at TEXT NOT NULL, source TEXT NOT NULL)""",
    "CREATE INDEX idx_quota_snapshots_observed_at ON quota_snapshots(observed_at)",
    """CREATE TABLE provider_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL, model TEXT,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL, event_type TEXT NOT NULL,
  http_status INTEGER, error_class TEXT, detail TEXT, created_at TEXT NOT NULL)""",
    "CREATE INDEX idx_provider_events_created_at ON provider_events(created_at)",
    """CREATE TABLE approvals (id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  session_id TEXT, discord_user_id TEXT, action_type TEXT NOT NULL,
  action_payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('PENDING','APPROVED','DENIED','EXPIRED')),
  requested_at TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT)""",
    """CREATE TABLE usage_events (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE, provider TEXT NOT NULL,
  model TEXT NOT NULL, request_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
  estimated_cost_usd REAL,
  cost_class TEXT NOT NULL CHECK (cost_class IN ('FREE','PAID')), status TEXT NOT NULL,
  occurred_at TEXT NOT NULL)""",
    "CREATE INDEX idx_usage_events_occurred_at ON usage_events(occurred_at)",
    """CREATE TABLE fallback_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, session_id TEXT,
  attempt_no INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL, error_class TEXT, http_status INTEGER, retry_after_ms INTEGER,
  started_at TEXT NOT NULL, finished_at TEXT,
  quota_snapshot_id INTEGER REFERENCES quota_snapshots(id) ON DELETE SET NULL,
  UNIQUE (task_id, attempt_no))""",
    "CREATE INDEX idx_fallback_attempts_started_at ON fallback_attempts(started_at)",
)

MIGRATIONS: tuple[Migration, ...] = (Migration(1, _V1),)
LATEST_VERSION: int = MIGRATIONS[-1].version


class SchemaTooNewError(RuntimeError):
    pass


def current_version(conn: sqlite3.Connection) -> int:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row[0] is not None else 0


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version("
        "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    version = current_version(conn)
    if version > LATEST_VERSION:
        raise SchemaTooNewError(
            f"database schema version {version} is newer than supported {LATEST_VERSION}"
        )
    for m in MIGRATIONS:
        if m.version <= version:
            continue
        with transaction(conn):
            for stmt in m.statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (m.version, to_db(utcnow())),
            )
        version = m.version
    return version
