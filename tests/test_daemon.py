from __future__ import annotations

import asyncio

import pytest

from app.config.loader import parse_config
from app.db.migrations import LATEST_VERSION
from app.db.tasks import TaskRepository
from app.orchestrator.state_machine import TaskStatus
from app.runtime.daemon import AgentController, ControllerEvent, TaskSubmission


def cfg_for(tmp_path, **runtime):
    return parse_config(
        {
            "database": {"path": str(tmp_path / "d" / "a.db")},
            "runtime": {"shutdown_timeout_s": 2, **runtime},
        }
    )


def sub(n=1):
    return TaskSubmission(project_id="p", prompt=f"do {n}", discord_user_id="u")


async def test_run_once(tmp_path):
    cfg = cfg_for(tmp_path)
    c = AgentController(cfg)
    await asyncio.wait_for(c.run_once(), 20)
    assert (tmp_path / "d" / "a.db").exists()
    assert c.metrics()["last_cleanup_at"] is not None
    import sqlite3

    conn = sqlite3.connect(tmp_path / "d" / "a.db")
    try:
        assert (
            conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == LATEST_VERSION
        )
    finally:
        conn.close()


async def test_submit_persisted_queued(tmp_path):
    c = AgentController(cfg_for(tmp_path))
    await c.start()
    try:
        assert c.submit_task(sub())
        await asyncio.wait_for(c._task_queue.join(), 5)
        rows = TaskRepository(c.conn).list_unfinished()
        assert len(rows) == 1 and rows[0].status == TaskStatus.QUEUED
    finally:
        await c.stop()


async def test_task_queue_bounded(tmp_path):
    gate = asyncio.Event()

    async def handler(ctrl, s):
        await gate.wait()

    c = AgentController(cfg_for(tmp_path, task_queue_maxsize=1), task_handler=handler)
    await c.start()
    try:
        results = [c.submit_task(sub(i)) for i in range(4)]
        assert results[-1] is False
        assert results.count(False) >= 1
    finally:
        gate.set()
        await c.stop()


def test_event_queue_bounded_unstarted(tmp_path):
    c = AgentController(cfg_for(tmp_path, event_queue_maxsize=1))
    assert c.emit(
        ControllerEvent("a", at=__import__("datetime").datetime.now(__import__("datetime").UTC))
    )
    ev = ControllerEvent("b", at=__import__("datetime").datetime.now(__import__("datetime").UTC))
    assert c.emit(ev) is False
    assert c.metrics()["event_dropped_count"] == 1
    assert c.metrics()["sqlite_db_bytes"] is None


async def test_handler_exception_does_not_kill_worker(tmp_path):
    calls = []

    async def handler(ctrl, s):
        calls.append(s.prompt)
        if len(calls) == 1:
            raise RuntimeError("boom")

    c = AgentController(cfg_for(tmp_path), task_handler=handler)
    await c.start()
    try:
        c.submit_task(sub(1))
        c.submit_task(sub(2))
        await asyncio.wait_for(c._task_queue.join(), 5)
        assert calls == ["do 1", "do 2"]
    finally:
        await c.stop()


async def test_stop_idempotent(tmp_path):
    c = AgentController(cfg_for(tmp_path))
    await c.start()
    await c.stop()
    await c.stop()
    assert c.metrics()["sqlite_db_bytes"] is None


async def test_start_twice_raises(tmp_path):
    c = AgentController(cfg_for(tmp_path))
    await c.start()
    try:
        with pytest.raises(RuntimeError):
            await c.start()
    finally:
        await c.stop()
