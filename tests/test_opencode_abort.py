"""Server-side session abort on cancel / timeout / outer cancellation (B11). Offline."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from app.config.models import OpenCodeConfig
from app.opencode.abort import abort_opencode_session
from app.opencode.executor import OpenCodeExecutor
from app.runtime.interfaces import ExecutionOutcome, ExecutorEventKind

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")
SERVER = "http://127.0.0.1:4096"


def make(tmp_path, scenario, handler, **cfg):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    extra = {
        "FAKE_OC_SCENARIOS": scenario,
        "FAKE_OC_ARGV_FILE": str(tmp_path / "argv.txt"),
        "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
        "FAKE_OC_HEARTBEAT": str(tmp_path / "hb.txt"),
    }
    config = OpenCodeConfig(command=[sys.executable, FAKE], kill_grace_s=1, **cfg)
    ex = OpenCodeExecutor(config, extra_env=extra, transport=httpx.MockTransport(wrapped))
    return ex, seen


def ok_handler(request):
    return httpx.Response(200, json=True)


def call(ex, tmp_path, cancel, on_event=None):
    return ex.run(
        task_id="t1",
        provider="openrouter",
        model="openrouter/free",
        prompt="do it",
        session_id=None,
        project_dir=str(tmp_path),
        cancel_event=cancel,
        on_event=on_event,
    )


def abort_requests(seen):
    return [r for r in seen if r.url.path.endswith("/abort")]


async def test_abort_sent_on_cancel(tmp_path):
    ex, seen = make(tmp_path, "hang", ok_handler)
    cancel = asyncio.Event()
    r = await asyncio.wait_for(
        call(
            ex,
            tmp_path,
            cancel,
            lambda e: cancel.set() if e.kind == ExecutorEventKind.SESSION else None,
        ),
        60,
    )
    assert r.outcome == ExecutionOutcome.CANCELLED
    aborts = abort_requests(seen)
    assert len(aborts) == 1
    assert aborts[0].method == "POST"
    assert aborts[0].url.path == "/session/ses_fake1/abort"


async def test_abort_sent_on_timeout(tmp_path):
    ex, seen = make(tmp_path, "hang", ok_handler, run_timeout_s=1)
    r = await asyncio.wait_for(call(ex, tmp_path, asyncio.Event()), 60)
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    aborts = abort_requests(seen)
    assert len(aborts) == 1 and aborts[0].url.path == "/session/ses_fake1/abort"


async def test_abort_sent_on_outer_cancellation(tmp_path):
    ex, seen = make(tmp_path, "hang", ok_handler)
    got_session = asyncio.Event()
    task = asyncio.create_task(
        call(
            ex,
            tmp_path,
            asyncio.Event(),
            lambda e: got_session.set() if e.kind == ExecutorEventKind.SESSION else None,
        )
    )
    await asyncio.wait_for(got_session.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 60)
    aborts = abort_requests(seen)
    assert len(aborts) == 1 and aborts[0].url.path == "/session/ses_fake1/abort"


async def test_no_abort_without_session_id(tmp_path):
    ex, seen = make(tmp_path, "hang_no_session", ok_handler)
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(1.5, cancel.set)
    r = await asyncio.wait_for(call(ex, tmp_path, cancel), 60)
    assert r.outcome == ExecutionOutcome.CANCELLED
    assert r.session_id is None
    assert seen == []


async def test_no_abort_on_normal_exit(tmp_path):
    ex, seen = make(tmp_path, "success", ok_handler)
    r = await asyncio.wait_for(call(ex, tmp_path, asyncio.Event()), 60)
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert abort_requests(seen) == []


def _cancel_run(tmp_path, handler, **cfg):
    ex, seen = make(tmp_path, "hang", handler, **cfg)
    cancel = asyncio.Event()
    fut = asyncio.wait_for(
        call(
            ex,
            tmp_path,
            cancel,
            lambda e: cancel.set() if e.kind == ExecutorEventKind.SESSION else None,
        ),
        60,
    )
    return fut, seen


async def test_abort_http_error_does_not_change_outcome(tmp_path):
    fut, seen = _cancel_run(tmp_path, lambda r: httpx.Response(500))
    r = await fut
    assert r.outcome == ExecutionOutcome.CANCELLED
    assert len(abort_requests(seen)) == 1


async def test_abort_connection_failure_does_not_change_outcome(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused")

    fut, seen = _cancel_run(tmp_path, boom)
    r = await fut
    assert r.outcome == ExecutionOutcome.CANCELLED
    assert len(abort_requests(seen)) == 1


async def test_abort_timeout_does_not_change_timeout_outcome(tmp_path):
    def slow(request):
        raise httpx.ReadTimeout("slow")

    ex, seen = make(tmp_path, "hang", slow, run_timeout_s=1)
    r = await asyncio.wait_for(call(ex, tmp_path, asyncio.Event()), 60)
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    assert len(abort_requests(seen)) == 1


async def test_abort_failure_on_outer_cancellation_still_raises_cancelled(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused")

    ex, seen = make(tmp_path, "hang", boom)
    got = asyncio.Event()
    task = asyncio.create_task(
        call(
            ex,
            tmp_path,
            asyncio.Event(),
            lambda e: got.set() if e.kind == ExecutorEventKind.SESSION else None,
        )
    )
    await asyncio.wait_for(got.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 60)
    assert len(abort_requests(seen)) == 1


@pytest.mark.parametrize(
    "sid", [None, "", "a/../b", "ses_1\n", "ses 1", "../x", "ses?x=1", "-bad", "a" * 200]
)
async def test_abort_rejects_invalid_session_ids(sid):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    ok = await abort_opencode_session(
        SERVER, sid, timeout_s=1, transport=httpx.MockTransport(handler)
    )
    assert ok is False and seen == []


async def test_abort_valid_id_and_trailing_slash_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(204)

    ok = await abort_opencode_session(
        SERVER + "/", "ses_abc-1", timeout_s=1, transport=httpx.MockTransport(handler)
    )
    assert ok is True
    assert [str(r.url) for r in seen] == [SERVER + "/session/ses_abc-1/abort"]


async def test_abort_never_logs_secrets(caplog):
    def boom(request):
        raise httpx.ConnectError("secret-token-xyz")

    with caplog.at_level("DEBUG"):
        ok = await abort_opencode_session(
            SERVER, "ses_1", timeout_s=1, transport=httpx.MockTransport(boom)
        )
    assert ok is False
    assert "secret-token-xyz" not in caplog.text
    assert "ConnectError" in caplog.text


@pytest.mark.parametrize("sid", [None, "", "a/../b", "ses_1\n", "ses 1", "../x", "ses?x=1"])
async def test_abort_invalid_ids_never_build_a_request(sid, monkeypatch):
    """Invalid ids must be rejected before any HTTP call is even attempted."""
    posted = []

    async def fake_post(self, url, *a, **kw):
        posted.append(url)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    ok = await abort_opencode_session(
        SERVER, sid, timeout_s=1, transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    assert ok is False
    assert posted == []


def call_resumed(ex, tmp_path, cancel, on_event=None):
    return ex.run(
        task_id="t1",
        provider="openrouter",
        model="openrouter/free",
        prompt="do it",
        session_id="ses_known1",
        project_dir=str(tmp_path),
        cancel_event=cancel,
        on_event=on_event,
    )


async def test_resumed_session_aborted_on_cancel_before_first_event(tmp_path):
    ex, seen = make(tmp_path, "hang_no_session", ok_handler)
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(1.5, cancel.set)
    r = await asyncio.wait_for(call_resumed(ex, tmp_path, cancel), 60)
    assert r.outcome == ExecutionOutcome.CANCELLED
    aborts = abort_requests(seen)
    assert len(aborts) == 1
    assert aborts[0].method == "POST"
    assert aborts[0].url.path == "/session/ses_known1/abort"


async def test_resumed_session_aborted_on_timeout_before_first_event(tmp_path):
    ex, seen = make(tmp_path, "hang_no_session", ok_handler, run_timeout_s=1)
    r = await asyncio.wait_for(call_resumed(ex, tmp_path, asyncio.Event()), 60)
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    aborts = abort_requests(seen)
    assert len(aborts) == 1 and aborts[0].url.path == "/session/ses_known1/abort"


async def test_resumed_session_aborted_on_outer_cancellation_before_first_event(tmp_path):
    ex, seen = make(tmp_path, "hang_no_session", ok_handler)
    task = asyncio.create_task(call_resumed(ex, tmp_path, asyncio.Event()))
    await asyncio.sleep(1.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 60)
    aborts = abort_requests(seen)
    assert len(aborts) == 1 and aborts[0].url.path == "/session/ses_known1/abort"


async def test_observed_session_id_preferred_over_passed_id(tmp_path):
    ex, seen = make(tmp_path, "hang", ok_handler)
    cancel = asyncio.Event()
    r = await asyncio.wait_for(
        call_resumed(
            ex,
            tmp_path,
            cancel,
            lambda e: cancel.set() if e.kind == ExecutorEventKind.SESSION else None,
        ),
        60,
    )
    assert r.outcome == ExecutionOutcome.CANCELLED
    aborts = abort_requests(seen)
    assert len(aborts) == 1 and aborts[0].url.path == "/session/ses_fake1/abort"
