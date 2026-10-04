from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.config.models import OpenCodeConfig
from app.opencode.executor import OpenCodeExecutor
from app.providers.base import ErrorClass
from app.redaction import SecretRedactor
from app.runtime.interfaces import ExecutionOutcome, ExecutorEventKind

FAKE = str(Path(__file__).parent / "fakes" / "fake_opencode.py")


def make_exec(tmp_path, scenarios="success", *, redactor=None, env=None, **cfg):
    extra = {
        "FAKE_OC_SCENARIOS": scenarios,
        "FAKE_OC_ARGV_FILE": str(tmp_path / "argv.txt"),
        "FAKE_OC_CWD_FILE": str(tmp_path / "cwd.txt"),
        "FAKE_OC_COUNTER_FILE": str(tmp_path / "counter.txt"),
        "FAKE_OC_HEARTBEAT": str(tmp_path / "hb.txt"),
    }
    extra.update(env or {})
    config = OpenCodeConfig(command=[sys.executable, FAKE], **cfg)
    return OpenCodeExecutor(
        config,
        redactor=redactor,
        extra_env=extra,
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
    )


async def run(ex, tmp_path, *, prompt="do it", session_id=None, cancel=None, on_event=None):
    return await asyncio.wait_for(
        ex.run(
            task_id="t1",
            provider="openrouter",
            model="openrouter/free",
            prompt=prompt,
            session_id=session_id,
            project_dir=str(tmp_path),
            cancel_event=cancel or asyncio.Event(),
            on_event=on_event,
        ),
        60,
    )


def argv_lines(tmp_path):
    p = tmp_path / "argv.txt"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_build_argv(tmp_path):
    ex = make_exec(tmp_path)
    argv = ex.build_argv("/bin/oc", provider="gemini", model="g-1", prompt="hi", session_id=None)
    assert argv[:2] == ["/bin/oc", FAKE]
    assert argv[2:5] == ["run", "--attach", "http://127.0.0.1:4096"]
    assert argv[0] == "/bin/oc" and argv[-1] == "hi"
    assert "--session" not in argv
    assert argv[argv.index("--model") + 1] == "google/g-1"
    assert argv[argv.index("--format") + 1] == "json"


def test_build_argv_session_and_openrouter_and_dash(tmp_path):
    ex = make_exec(tmp_path)
    assert ex.opencode_model("openrouter", "openrouter/free") == "openrouter/openrouter/free"
    assert ex.opencode_model("unmapped", "m") == "unmapped/m"
    argv = ex.build_argv("x", provider="groq", model="m", prompt="-rf", session_id="ses_1")
    assert argv[argv.index("--session") + 1] == "ses_1"
    assert argv[-1] == " -rf"


async def test_success(tmp_path):
    ex = make_exec(tmp_path)
    events = []
    r = await run(ex, tmp_path, on_event=events.append)
    assert r.outcome == ExecutionOutcome.SUCCEEDED and r.exit_code == 0
    assert r.session_id == "ses_fake1"
    assert r.changed_files == ("src/a.py",)
    assert (r.input_tokens, r.output_tokens) == (10, 5)
    assert r.llm_steps == 1 and r.tool_events == 1
    assert "done" in r.output_tail
    assert Path((tmp_path / "cwd.txt").read_text().strip()).resolve() == tmp_path.resolve()
    argv = argv_lines(tmp_path)[0]
    assert argv[argv.index("--format") + 1] == "json"
    kinds = [e.kind for e in events]
    assert kinds[0] == ExecutorEventKind.SESSION and ExecutorEventKind.TOOL in kinds
    assert ExecutorEventKind.STEP in kinds


async def test_session_arg_passed(tmp_path):
    r = await run(make_exec(tmp_path), tmp_path, session_id="ses_prev")
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    argv = argv_lines(tmp_path)[0]
    assert argv[argv.index("--session") + 1] == "ses_prev"


async def test_rate_limit(tmp_path):
    r = await run(make_exec(tmp_path, "rate_limit"), tmp_path)
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.RATE_LIMITED
    assert r.http_status == 429 and r.retry_after_s == 30
    assert r.session_id == "ses_fake1"


async def test_quota_text(tmp_path):
    r = await run(make_exec(tmp_path, "quota_text"), tmp_path)
    assert r.error_class == ErrorClass.QUOTA_EXHAUSTED


async def test_auth(tmp_path):
    r = await run(make_exec(tmp_path, "auth"), tmp_path)
    assert r.error_class == ErrorClass.AUTH_FAILED and r.session_id is None


async def test_exit_nonzero(tmp_path):
    r = await run(make_exec(tmp_path, "exit_nonzero"), tmp_path)
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.UNKNOWN and r.exit_code == 3
    assert "boom" in (r.error_message or "")


async def test_error_with_exit0(tmp_path):
    r = await run(make_exec(tmp_path, "error_exit0"), tmp_path)
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.PROVIDER_UNAVAILABLE


async def test_garbage_lines_ignored(tmp_path):
    r = await run(make_exec(tmp_path, "garbage"), tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED


async def test_timeout(tmp_path):
    ex = make_exec(tmp_path, "hang", run_timeout_s=1, kill_grace_s=1)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    r = await run(ex, tmp_path)
    assert loop.time() - t0 < 30
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    assert r.error_class == ErrorClass.TIMEOUT_UNKNOWN_OUTCOME
    assert r.session_id == "ses_fake1"


async def test_cancel(tmp_path):
    ex = make_exec(tmp_path, "hang", kill_grace_s=1)
    cancel = asyncio.Event()
    r = await run(
        ex,
        tmp_path,
        cancel=cancel,
        on_event=lambda e: cancel.set() if e.kind == ExecutorEventKind.SESSION else None,
    )
    assert r.outcome == ExecutionOutcome.CANCELLED and r.error_class is None


async def _hb_size(tmp_path) -> int:
    p = tmp_path / "hb.txt"
    return p.stat().st_size if p.exists() else 0


async def test_cancel_kills_process_tree(tmp_path):
    ex = make_exec(tmp_path, "spawn_child", kill_grace_s=1)
    cancel = asyncio.Event()
    hb = tmp_path / "hb.txt"

    async def trigger():
        for _ in range(100):
            if hb.exists() and hb.stat().st_size > 0:
                break
            await asyncio.sleep(0.1)
        cancel.set()

    t = asyncio.create_task(trigger())
    r = await run(ex, tmp_path, cancel=cancel)
    await t
    assert r.outcome == ExecutionOutcome.CANCELLED
    await asyncio.sleep(1)
    size = await _hb_size(tmp_path)
    assert size > 0
    await asyncio.sleep(1)
    assert await _hb_size(tmp_path) == size


async def test_outer_cancel_kills_tree_and_propagates(tmp_path):
    ex = make_exec(tmp_path, "spawn_child", kill_grace_s=1)
    hb = tmp_path / "hb.txt"
    task = asyncio.create_task(run(ex, tmp_path))
    for _ in range(100):
        if hb.exists() and hb.stat().st_size > 0:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1)
    size = await _hb_size(tmp_path)
    await asyncio.sleep(1)
    assert await _hb_size(tmp_path) == size


async def test_flood_bounded(tmp_path):
    ex = make_exec(tmp_path, "flood", output_tail_chars=500)
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert len(r.output_tail) <= 500 and r.output_truncated
    assert r.llm_steps == 0


async def test_long_line_skipped(tmp_path):
    r = await run(make_exec(tmp_path, "long_line", max_line_bytes=1_048_576), tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert r.session_id == "ses_fake1"


async def test_secret_redacted(tmp_path):
    secret = "sk-test-SECRET-abcdef123456"
    ex = make_exec(
        tmp_path, "secret_echo", redactor=SecretRedactor([secret]), env={"FAKE_OC_SECRET": secret}
    )
    r = await run(ex, tmp_path)
    assert secret not in r.output_tail and "[REDACTED]" in r.output_tail


async def test_missing_executable(tmp_path):
    ex = OpenCodeExecutor(OpenCodeConfig(command=["definitely-not-a-real-binary-xyz"]))
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert r.detail == "opencode executable not found"


async def test_missing_project_dir(tmp_path):
    ex = make_exec(tmp_path)
    r = await asyncio.wait_for(
        ex.run(
            task_id="t",
            provider="p",
            model="m",
            prompt="x",
            session_id=None,
            project_dir=str(tmp_path / "nope"),
            cancel_event=asyncio.Event(),
        ),
        30,
    )
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert r.detail == "project directory missing"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only launcher check")
async def test_cmd_launcher_refused(tmp_path):
    cmd = tmp_path / "oc.cmd"
    cmd.write_text("@echo off\r\n")
    ex = OpenCodeExecutor(OpenCodeConfig(command=[str(cmd)]))
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert ".cmd" in (r.detail or "")


async def test_invalid_prompts_do_not_spawn(tmp_path):
    ex = make_exec(tmp_path)
    for bad in ("", "   \x00 ", "x" * 4001):
        r = await run(ex, tmp_path, prompt=bad)
        assert r.outcome == ExecutionOutcome.FAILED
        assert r.error_class == ErrorClass.INVALID_REQUEST
    r = await run(ex, tmp_path, session_id="-bad id")
    assert r.error_class == ErrorClass.INVALID_REQUEST
    assert not (tmp_path / "argv.txt").exists()


async def test_prompt_nul_stripped_and_dash_prefixed(tmp_path):
    r = await run(make_exec(tmp_path), tmp_path, prompt="--help\x00 me ")
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert argv_lines(tmp_path)[0][-1] == " --help me"
