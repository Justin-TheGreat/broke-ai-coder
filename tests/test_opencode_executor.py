from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from app.config.models import OpenCodeConfig
from app.opencode.executor import OpenCodeExecutor
from app.opencode.os_user import DirectLauncher
from app.providers.base import ErrorClass
from app.redaction import SecretRedactor
from app.runtime.interfaces import ExecutionOutcome, ExecutorEventKind, StartupError

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
    assert argv[argv.index("--model") + 1] == "broke/gemini/g-1"
    assert argv[argv.index("--format") + 1] == "json"


def test_build_argv_session_and_openrouter_and_dash(tmp_path):
    ex = make_exec(tmp_path)
    assert ex.opencode_model("openrouter", "openrouter/free") == "broke/openrouter/openrouter/free"
    assert ex.opencode_model("groq", "m") == "broke/groq/m"
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
async def test_cmd_launcher_refused(tmp_path, monkeypatch):
    cmd = tmp_path / "oc.cmd"
    cmd.write_text("@echo off\r\n")
    monkeypatch.setattr("app.opencode.process.shutil.which", lambda _c: str(cmd))
    ex = OpenCodeExecutor(OpenCodeConfig(command=["opencode"]))
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


@pytest.mark.parametrize(
    "bad",
    ["ses_ok\n", "ses_ok\r\n", "ses_ok ", " ses_ok", "ses_ok\x00", "", "ses ok", "-ses", "a" * 129],
)
async def test_session_id_fullmatch_failures_rejected_before_spawn(tmp_path, bad):
    r = await run(make_exec(tmp_path), tmp_path, session_id=bad)
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.INVALID_REQUEST
    assert not (tmp_path / "argv.txt").exists()


async def test_valid_session_id_still_spawns(tmp_path):
    r = await run(make_exec(tmp_path), tmp_path, session_id="ses_ok-1")
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert (tmp_path / "argv.txt").exists()


async def test_session_id_trailing_newline_rejected_before_spawn(tmp_path):
    r = await run(make_exec(tmp_path), tmp_path, session_id="ses_ok\n")
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.INVALID_REQUEST
    assert not (tmp_path / "argv.txt").exists()


@pytest.mark.parametrize(
    "command",
    [
        ["cmd", "/c", "opencode"],
        ["CMD.EXE", "/c", "opencode"],
        ["powershell", "-Command", "opencode"],
        ["C:\\Windows\\System32\\pwsh.exe", "opencode"],
        ["/bin/bash", "-c", "opencode"],
        ["sh", "-c", "opencode"],
        ["zsh", "opencode"],
        ["dash", "opencode"],
        ["wsl", "opencode"],
        ["env", "opencode"],
        ["start", "opencode"],
        ["opencode", "/usr/bin/env", "x"],
        ["C:/tools/oc.cmd"],
        ["oc.BAT"],
        ["node", "run.ps1"],
        ["CMD", "/c", "opencode"],
        ["C:\\x\\cmd.exe", "/c", "opencode"],
        ["C:\\WINDOWS\\SYSTEM32\\CMD.EXE", "/C", "opencode"],
        ["Powershell.EXE", "-c", "opencode"],
        ["C:/Program Files/PowerShell/7/pwsh.exe", "opencode"],
        ["BASH.exe", "-c", "opencode"],
        ["/usr/bin/env", "opencode"],
        ["C:\\Windows\\System32\\wsl.exe", "opencode"],
        ["start.exe", "opencode"],
        ["opencode", "x.PS1"],
        ["opencode", "C:\\a\\b.Bat"],
        ["opencode", "c:/a/run.CMD"],
        ["  cmd  ", "/c", "opencode"],
    ],
)
def test_command_rejects_shells_and_scripts(command):
    with pytest.raises(ValueError):
        OpenCodeConfig(command=command)


@pytest.mark.parametrize(
    "command",
    [
        ["opencode"],
        ["opencode.exe", "serve"],
        ["python", "-m", "x"],
        ["python3.12"],
        ["node", "x.js"],
    ],
)
def test_command_allows_plain_launchers(command):
    assert OpenCodeConfig(command=command).command == command


async def test_child_env_is_an_allowlist_and_parent_environ_unchanged(monkeypatch):
    import os

    monkeypatch.setenv("TEST_BOT_TOKEN_VAR", "tok")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("LC_ALL", "C")
    ex = OpenCodeExecutor(OpenCodeConfig(command=[sys.executable, FAKE]), extra_env={"X": "1"})
    env = ex._child_env()
    assert "TEST_BOT_TOKEN_VAR" not in env and "OPENROUTER_API_KEY" not in env
    assert env["X"] == "1" and env["LC_ALL"] == "C"
    assert os.environ["TEST_BOT_TOKEN_VAR"] == "tok"


async def test_child_env_server_password_and_passthrough(monkeypatch):
    from app.opencode.endpoint import ServerEndpoint

    cfg = OpenCodeConfig(command=[sys.executable, FAKE], env_passthrough=["MY_PROXY"])
    ex = OpenCodeExecutor(
        cfg,
        endpoint=lambda: ServerEndpoint("http://127.0.0.1:1", "pw-123456", "opencode"),
        environ={"MY_PROXY": "p", "GROQ_API_KEY": "nope", "PATH": "/bin"},
    )
    env = ex._child_env()
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw-123456"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["MY_PROXY"] == "p" and "GROQ_API_KEY" not in env
    plain = OpenCodeExecutor(cfg, environ={})._child_env()
    assert "OPENCODE_SERVER_PASSWORD" not in plain


async def test_attach_uses_endpoint_url_and_auth_for_health_and_abort(tmp_path):
    from app.opencode.endpoint import ServerEndpoint

    seen = []

    def handler(req):
        seen.append((str(req.url), req.headers.get("authorization")))
        return httpx.Response(200, json={"healthy": True, "version": "v"})

    ep = ServerEndpoint("http://127.0.0.1:5151", "pw-123456")
    ex = OpenCodeExecutor(
        OpenCodeConfig(command=[sys.executable, FAKE]),
        endpoint=lambda: ep,
        transport=httpx.MockTransport(handler),
    )
    argv = ex.build_argv("x", provider="groq", model="m", prompt="hi", session_id=None)
    assert argv[argv.index("--attach") + 1] == "http://127.0.0.1:5151"
    assert (await ex.health_check()).healthy
    await ex._abort_session("ses_1")
    assert [u for u, _ in seen] == [
        "http://127.0.0.1:5151/global/health",
        "http://127.0.0.1:5151/session/ses_1/abort",
    ]
    assert all(a and a.startswith("Basic ") for _, a in seen)


def test_command_allows_python_and_node():
    assert OpenCodeConfig(command=[sys.executable, FAKE]).command[0] == sys.executable
    assert OpenCodeConfig(command=["node", "C:/path/opencode/bin/opencode"])


async def test_bot_token_and_provider_keys_not_passed_to_child(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_BOT_TOKEN_VAR", "tok-should-not-leak")
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "key-should-not-leak")
    monkeypatch.setenv("TEST_PLAIN_VAR", "kept-only-via-passthrough")
    for var, expected in (
        ("TEST_BOT_TOKEN_VAR", "present=False"),
        ("TEST_PROVIDER_API_KEY", "present=False"),
        ("TEST_PLAIN_VAR", "present=False"),
    ):
        ex = OpenCodeExecutor(
            OpenCodeConfig(command=[sys.executable, FAKE]),
            extra_env={"FAKE_OC_SCENARIOS": "env_dump", "FAKE_OC_BOT_VAR": var},
        )
        assert (await run(ex, tmp_path)).output_tail.strip() == expected
    ex2 = OpenCodeExecutor(
        OpenCodeConfig(command=[sys.executable, FAKE], env_passthrough=["TEST_PLAIN_VAR"]),
        extra_env={"FAKE_OC_SCENARIOS": "env_dump", "FAKE_OC_BOT_VAR": "TEST_PLAIN_VAR"},
    )
    assert (await run(ex2, tmp_path)).output_tail.strip() == "present=True"


async def test_prompt_nul_stripped_and_dash_prefixed(tmp_path):
    r = await run(make_exec(tmp_path), tmp_path, prompt="--help\x00 me ")
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    assert argv_lines(tmp_path)[0][-1] == " --help me"


# ---- launcher integration and N9 -------------------------------------------------------


class RecordingLauncher(DirectLauncher):
    def __init__(self, prefix: list[str] | None = None) -> None:
        super().__init__()
        self.prefix = prefix or []
        self.kills: list[float] = []

    def command(self, argv):
        return [*self.prefix, *argv]

    async def kill_tree(self, proc, *, grace_s):
        self.kills.append(grace_s)
        await super().kill_tree(proc, grace_s=grace_s)


def make_exec_with(tmp_path, scenarios="success", *, launcher=None, reason=None, **cfg):
    ex = make_exec(tmp_path, scenarios, **cfg)
    ex._launcher = launcher or RecordingLauncher()
    ex._unavailable_reason = reason
    return ex


async def test_n9_unavailable_reason_blocks_spawn(tmp_path, monkeypatch):
    async def no_spawn(*a, **k):
        raise AssertionError("must not spawn while the managed serve is down")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    ex = make_exec_with(tmp_path, reason=lambda: "managed OpenCode server is down: restarting")
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert r.detail == "managed OpenCode server is down: restarting"


async def test_n9_invalid_prompt_still_invalid(tmp_path):
    ex = make_exec_with(tmp_path, reason=lambda: "down")
    r = await run(ex, tmp_path, prompt="   ")
    assert r.outcome == ExecutionOutcome.FAILED
    assert r.error_class == ErrorClass.INVALID_REQUEST


async def test_n9_reason_none_spawns_normally(tmp_path):
    r = await run(make_exec_with(tmp_path, reason=lambda: None), tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED


async def test_spawned_argv_is_launcher_command_of_build_argv(tmp_path, monkeypatch):
    ex = make_exec_with(tmp_path, launcher=RecordingLauncher(prefix=["PFX", "--"]))
    seen: dict = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        seen["argv"] = list(argv)
        return await real(*argv[2:], **kw)  # drop the fake prefix to really run

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED
    expected = ex._launcher.command(
        ex.build_argv(
            seen["argv"][2],
            provider="openrouter",
            model="openrouter/free",
            prompt="do it",
            session_id=None,
        )
    )
    assert seen["argv"] == expected
    assert seen["argv"][:2] == ["PFX", "--"]


async def test_timeout_uses_launcher_kill_tree(tmp_path):
    launcher = RecordingLauncher()
    ex = make_exec_with(tmp_path, "hang", launcher=launcher, run_timeout_s=1, kill_grace_s=1)
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.TIMED_OUT
    assert launcher.kills == [1]


async def test_unavailable_when_launcher_rejects_executable(tmp_path):
    class Rejecting(RecordingLauncher):
        def executable(self, command0):
            raise StartupError("opencode.command[0] must be an absolute path when x")

    ex = make_exec_with(tmp_path, launcher=Rejecting())
    r = await run(ex, tmp_path)
    assert r.outcome == ExecutionOutcome.EXECUTOR_UNAVAILABLE
    assert "absolute path" in r.detail


async def test_check_server_config_uses_launcher_auth_problem(tmp_path):
    class Problem(RecordingLauncher):
        async def auth_store_problem(self):
            return "OpenCode has stored provider credentials for: openai. launcher-said-so"

    ex = make_exec_with(tmp_path, launcher=Problem())
    check, detail = await ex.check_server_config(frozenset({"broke/x"}))
    assert check.value == "UNSAFE" and "launcher-said-so" in detail


# ---- S4: stray sweep after every run ---------------------------------------------------


class StrayLauncher(RecordingLauncher):
    def __init__(self) -> None:
        super().__init__()
        self.strays = 0

    async def sweep_stray(self):
        self.strays += 1


@pytest.mark.parametrize(
    ("scenario", "cfg", "outcome"),
    [
        ("success", {}, ExecutionOutcome.SUCCEEDED),
        ("hang", {"run_timeout_s": 1, "kill_grace_s": 1}, ExecutionOutcome.TIMED_OUT),
    ],
)
async def test_stray_sweep_runs_once_after_each_run(tmp_path, scenario, cfg, outcome):
    launcher = StrayLauncher()
    r = await run(make_exec_with(tmp_path, scenario, launcher=launcher, **cfg), tmp_path)
    assert r.outcome == outcome
    assert launcher.strays == 1


async def test_stray_sweep_failure_does_not_break_the_run(tmp_path):
    class Boom(StrayLauncher):
        async def sweep_stray(self):
            raise RuntimeError("sudo exploded")

    r = await run(make_exec_with(tmp_path, launcher=Boom()), tmp_path)
    assert r.outcome == ExecutionOutcome.SUCCEEDED


async def test_no_stray_sweep_when_run_never_spawns(tmp_path):
    launcher = StrayLauncher()
    ex = make_exec_with(tmp_path, launcher=launcher, reason=lambda: "down")
    await run(ex, tmp_path)
    assert launcher.strays == 0
