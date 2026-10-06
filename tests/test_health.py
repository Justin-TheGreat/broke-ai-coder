"""T093/T094/T024: /health diagnostics, process self-metrics and launcher status summaries."""

from __future__ import annotations

import sys
from datetime import timedelta

from conftest import NOW, secret_env
from fakes.fake_executor import ScriptedExecutor, ok
from test_opencode_os_user import launcher

from app.bot.backend import ControllerBackend
from app.bot.commands import HealthSnapshot
from app.bot.format import format_health
from app.opencode.os_user import DirectLauncher
from app.orchestrator.orchestrator import Orchestrator
from app.redaction import SecretRedactor
from app.runtime import resources
from app.runtime.daemon import AgentController
from app.runtime.diagnostics import RuntimeProbes
from app.runtime.resources import ProcessSampler, ResourceSample
from app.timeutil import utcnow

# ---- resources --------------------------------------------------------------------------


def test_read_rss_bytes_from_fake_status(tmp_path):
    p = tmp_path / "status"
    p.write_text("Name:\tx\nVmPeak:\t 99 kB\nVmRSS:\t   2048 kB\nThreads:\t1\n", encoding="ascii")
    assert resources.read_rss_bytes(str(p)) == 2048 * 1024
    p.write_text("VmRSS:\tnot-a-number kB\n", encoding="ascii")
    assert resources.read_rss_bytes(str(p)) is None
    p.write_text("Name:\tx\n", encoding="ascii")
    assert resources.read_rss_bytes(str(p)) is None


def test_read_rss_bytes_missing_path_is_none(tmp_path):
    assert resources.read_rss_bytes(str(tmp_path / "nope")) is None


def test_peak_rss_none_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert resources.read_peak_rss_bytes() is None


def test_cpu_seconds_is_float():
    assert isinstance(resources.cpu_seconds(), float)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.cpu = 5.0

    def __call__(self):
        return self.now


def test_sampler_percentages_and_windows():
    c = Clock()
    s = ProcessSampler(clock=c, cpu=lambda: c.cpu, rss=lambda: 1_048_576, peak=lambda: 2_097_152)
    c.now, c.cpu = 100.2, 5.1
    first = s.sample()
    assert first.cpu_percent_since_start is None and first.cpu_percent_window is None
    assert first.rss_bytes == 1_048_576 and first.peak_rss_bytes == 2_097_152
    c.now, c.cpu = 110.0, 6.0  # 10 s since start, 1 s of CPU
    second = s.sample()
    assert round(second.cpu_percent_since_start, 3) == 10.0
    assert round(second.cpu_percent_window, 3) == round(100 * 0.9 / 9.8, 3)
    assert round(second.window_s, 6) == 9.8 and second.uptime_s == 10.0
    c.now, c.cpu = 110.5, 6.1  # window shorter than 1 s
    third = s.sample()
    assert third.cpu_percent_window is None and third.cpu_percent_since_start is not None


def test_sampler_never_raises():
    def boom():
        raise RuntimeError("x")

    s = ProcessSampler(cpu=boom, rss=boom, peak=boom)
    out = s.sample()
    assert out.rss_bytes is None and out.peak_rss_bytes is None and out.cpu_seconds_total == 0.0


# ---- format_health ----------------------------------------------------------------------


def snap(**kw):
    base = {
        "resources": ResourceSample(
            rss_bytes=int(85.2 * 1_048_576),
            peak_rss_bytes=int(90.1 * 1_048_576),
            cpu_seconds_total=12.3,
            uptime_s=2 * 3600 + 180,
            cpu_percent_since_start=0.3,
            cpu_percent_window=0.4,
            window_s=120.0,
        ),
        "sqlite_db_bytes": int(1.2 * 1_048_576),
        "last_cleanup_at": "2026-10-06T10:00:00Z",
        "task_queue_depth": 0,
        "event_queue_depth": 0,
        "event_dropped_count": 0,
        "running_task_id": None,
        "provider_cooldowns": 1,
        "model_cooldowns": 2,
        "gateway_up": True,
        "serve_ready": True,
        "serve_fatal_error": None,
        "os_user": "sudo as brokeoc: ok",
    }
    base.update(kw)
    return HealthSnapshot(**base)


def test_format_health_full_example():
    text = format_health(snap())
    lines = text.splitlines()
    assert lines[0] == (
        "Controller health (daemon process only; excludes OpenCode and project child processes)"
    )
    assert lines[1] == "RSS: 85.2 MB (peak 90.1 MB); targets idle < 250 MB, normal < 512 MB"
    assert lines[2].startswith(
        "CPU: 0.4% of one core over the last 120 s; 0.3% since start (12.3 s"
    )
    assert "up 2h 03m" in lines[2] and lines[2].endswith("targets idle < 2%, normal < 10%")
    assert lines[3] == "SQLite: 1.2 MB; last cleanup 2026-10-06T10:00:00Z"
    assert lines[4] == "Queues: tasks 0, events 0 (dropped 0); running task: none"
    assert lines[5] == "Provider cooldowns: 1 provider-wide, 2 model"
    assert lines[6:] == ["Gateway: up", "OpenCode serve: ready", "OS user: sudo as brokeoc: ok"]


def test_format_health_unavailable_and_unknowns():
    r = ResourceSample(None, None, 1.0, 10.0, None, None, 0.0)
    text = format_health(
        snap(
            resources=r,
            last_cleanup_at=None,
            running_task_id="abcdef1234567890",
            gateway_up=None,
            serve_ready=None,
            os_user=None,
        )
    )
    assert "RSS: unavailable on this platform;" in text
    assert "over the last" not in text
    assert "last cleanup never" in text and "running task: abcdef12\n" in text + "\n"
    assert "Gateway: unknown" in text
    assert "OpenCode serve: not managed (opencode.managed_server: false)" in text
    assert "OS user: unknown" in text
    assert "Resources: unavailable" in format_health(snap(resources=None))
    assert "Gateway: DOWN" in format_health(snap(gateway_up=False))
    assert "OpenCode serve: down (restarting)" in format_health(snap(serve_ready=False))
    no_peak = ResourceSample(1_048_576, None, 1.0, 10.0, None, None, 0.0)
    assert "RSS: 1.0 MB;" in format_health(snap(resources=no_peak))


def test_format_health_fatal_truncated_and_redacted():
    secret = "sk-test-SECRET-0123456789"
    red = SecretRedactor([secret])
    text = format_health(
        snap(
            serve_ready=False,
            serve_fatal_error=f"{secret} " + "x" * 400,
            os_user=f"user {secret}",
        ),
        redactor=red,
    )
    assert secret not in text
    fatal = next(ln for ln in text.splitlines() if ln.startswith("OpenCode serve: FATAL"))
    assert fatal.startswith("OpenCode serve: FATAL (restart the daemon): ")
    assert len(fatal) <= len("OpenCode serve: FATAL (restart the daemon): ") + 200 + 20


# ---- ControllerBackend.health -----------------------------------------------------------


async def test_controller_backend_health_with_fake_probes(tmp_path, make_config):
    cfg = make_config(database={"path": str(tmp_path / "h.db")})
    orch = Orchestrator(
        cfg,
        executor=ScriptedExecutor([ok()]),
        adapters={},
        environ=secret_env(),
        clock=lambda: NOW,
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    try:
        until = utcnow() + timedelta(hours=1)
        orch.cooldowns.set_cooldown("groq", None, until)
        orch.cooldowns.set_cooldown("groq", "m1", until)
        orch.cooldowns.set_cooldown("gemini", "m2", until)
        probes = RuntimeProbes(
            gateway_up=lambda: True,
            serve_state=lambda: (False, "boom"),
            os_user_summary=lambda: "summary",
        )
        h = ControllerBackend(c, cfg, probes=probes).health()
        assert h.provider_cooldowns == 1 and h.model_cooldowns == 2
        assert h.gateway_up is True and h.serve_ready is False and h.serve_fatal_error == "boom"
        assert h.os_user == "summary" and h.resources is not None
        assert h.sqlite_db_bytes and h.task_queue_depth == 0 and h.running_task_id is None
        bare = ControllerBackend(c, cfg).health()
        assert bare.resources is None and bare.gateway_up is None and bare.serve_ready is None
        assert bare.os_user is None
    finally:
        await c.stop()


# ---- launcher status summaries ----------------------------------------------------------


def test_direct_launcher_status_summary():
    s = DirectLauncher().status_summary()
    assert s.startswith("mode none:") and "unsupported for production" in s


async def test_sudo_launcher_status_summary(make_config):
    sl, probe, _cfg = launcher(make_config)
    assert sl.status_summary() == "sudo as brokeoc: self-check not run yet"
    probe.dirs = {"/var/lib/broke-ai-coder/opencode-server"}
    await sl.start()
    text = sl.status_summary()
    assert text.startswith("sudo as brokeoc: isolation self-check passed at ")
    assert text.endswith(" UTC; WSL interop now disabled")
    probe.interop = True
    assert sl.status_summary().endswith("WSL interop now ENABLED (unsafe; restart required)")

    def boom():
        raise RuntimeError("x")

    probe.interop_enabled = boom
    assert sl.status_summary().endswith("WSL interop now unknown")


# ---- audit additions: secret-free output, dispatch redaction, unauthorized users -------------

SECRETS = {
    "bot": "DISCORD-BOT-TOKEN-ABCDEF123456",
    "key": "sk-or-v1-PROVIDERKEY0123456789",
    "gateway": "gateway-token-0123456789abcdef",
    "password": "serve-password-0123456789abcdef",
}


def _everything_redactor() -> SecretRedactor:
    return SecretRedactor(list(SECRETS.values()))


async def test_health_through_dispatch_redacts_every_secret_and_has_no_urls_or_paths(
    tmp_path, make_config
):
    from test_bot_commands import CFG, OK

    from app.bot.commands import run_command

    cfg = make_config(database={"path": str(tmp_path / "secret-dir" / "h.db")})
    orch = Orchestrator(
        cfg,
        executor=ScriptedExecutor([ok()]),
        adapters={},
        environ=secret_env(),
        clock=lambda: NOW,
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    try:
        # Every free-text probe is hostile: it carries all secrets, an endpoint and a path.
        leak = " ".join(SECRETS.values()) + " http://127.0.0.1:4096/ /etc/shadow"
        probes = RuntimeProbes(
            gateway_up=lambda: True,
            serve_state=lambda: (False, leak),
            os_user_summary=lambda: leak,
        )
        backend = ControllerBackend(c, cfg, probes=probes)
        reply = await run_command(
            "health",
            cfg=CFG,
            backend=backend,
            ctx=OK,
            redactor=_everything_redactor(),
            options={},
        )
    finally:
        await c.stop()
    assert reply.ephemeral
    for name, value in SECRETS.items():
        assert value not in reply.content, name
    assert "[REDACTED]" in reply.content
    assert "excludes OpenCode" in reply.content
    # nothing the controller itself knows leaks: database path, tmp dir
    assert str(tmp_path) not in reply.content and "secret-dir" not in reply.content


async def test_health_from_real_backend_without_hostile_probes_has_no_endpoint_or_path(
    tmp_path, make_config, monkeypatch
):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", SECRETS["bot"])
    monkeypatch.setenv("OPENROUTER_API_KEY", SECRETS["key"])
    cfg = make_config(database={"path": str(tmp_path / "h.db")})
    orch = Orchestrator(
        cfg,
        executor=ScriptedExecutor([ok()]),
        adapters={},
        environ=secret_env(),
        clock=lambda: NOW,
    )
    c = AgentController(cfg, orchestrator=orch)
    await c.start()
    try:
        text = format_health(
            ControllerBackend(
                c,
                cfg,
                probes=RuntimeProbes(gateway_up=lambda: True, serve_state=lambda: (True, None)),
            ).health(),
            redactor=_everything_redactor(),
        )
    finally:
        await c.stop()
    assert "http://" not in text and "https://" not in text and "127.0.0.1" not in text
    assert str(tmp_path) not in text and "h.db" not in text
    for value in SECRETS.values():
        assert value not in text
    assert "password" not in text.lower() and "token" not in text.lower()
    assert text.splitlines()[0].startswith("Controller health (daemon process only; excludes")


def test_format_health_graceful_when_platform_has_no_metrics(monkeypatch):
    # Windows: /proc is missing and `resource` is unavailable, so every reader returns None.
    monkeypatch.setattr(sys, "platform", "win32")
    assert resources.read_rss_bytes("/definitely/not/here") is None
    assert resources.read_peak_rss_bytes() is None
    s = ProcessSampler(rss=lambda: None, peak=lambda: None).sample()
    text = format_health(snap(resources=s))
    assert "RSS: unavailable on this platform;" in text
    assert "excludes OpenCode" in text


async def test_health_unauthorized_never_reaches_backend_or_probes():
    from test_bot_commands import CFG, FakeBackend

    from app.bot.commands import CommandContext, run_command

    b = FakeBackend()
    r = await run_command(
        "health", cfg=CFG, backend=b, ctx=CommandContext(99, 2, 3), redactor=None, options={}
    )
    assert r.content == "Not authorized." and r.ephemeral and b.calls == []
