"""Review-fix coverage: /proc scan, sweep_stray argv safety, TASKS helper, fake lifetime cap."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import app.opencode.os_user as ou
from tests.test_opencode_os_user import OC, PKILL, launcher, stat_line

ROOT = Path(__file__).resolve().parent.parent
FAKE = str(ROOT / "tests" / "fakes" / "fake_opencode.py")


# ---- parse_stat_session edge cases -------------------------------------------------------


def test_parse_stat_comm_with_newline_and_parens():
    assert ou.parse_stat_session(stat_line(9, "a\nb) (c\n", 123)) == 123


@pytest.mark.parametrize(
    "raw",
    ["", b"", "(", ")", "1 (x)", "1 (x) S", "1 (x) S 1 2", b"\xff\xfe\x00", "1 (x) S 1 2 3.5"],
)
def test_parse_stat_malformed_returns_none(raw):
    assert ou.parse_stat_session(raw) is None


# ---- SystemHostProbe.user_processes against a fake /proc ----------------------------------


class _Redirect:
    """Stand-in for pathlib.Path that maps /proc onto a temp tree."""

    root: Path

    def __new__(cls, *parts):
        if parts and str(parts[0]) == "/proc":
            return Path(cls.root, *parts[1:])
        return Path(*parts)


def _mk(root: Path, pid: str, ruid: int, euid: int, stat: str | None, status_ok=True):
    d = root / pid
    d.mkdir()
    if status_ok:
        (d / "status").write_text(f"Name:\tx\nUid:\t{ruid}\t{euid}\t{euid}\t{euid}\nGid:\t1\n")
    if stat is not None:
        (d / "stat").write_text(stat)


@pytest.fixture
def fake_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    _Redirect.root = root
    monkeypatch.setattr(ou, "Path", _Redirect)
    real_listdir = os.listdir
    monkeypatch.setattr(
        ou.os, "listdir", lambda p: real_listdir(root) if str(p) == "/proc" else real_listdir(p)
    )
    return root


def test_user_processes_filters_by_real_uid_and_skips_bad_entries(fake_proc):
    _mk(fake_proc, "100", 1001, 1001, stat_line(100, "opencode", 100))
    _mk(fake_proc, "101", 1001, 0, stat_line(101, "a) b", 100))  # real=oc, effective=root
    _mk(fake_proc, "102", 0, 1001, stat_line(102, "suid", 102))  # real=root, effective=oc
    _mk(fake_proc, "103", 1000, 1000, stat_line(103, "other", 103))
    _mk(fake_proc, "104", 1001, 1001, None)  # stat vanished
    _mk(fake_proc, "105", 1001, 1001, "garbage")  # unparseable
    _mk(fake_proc, "106", 1001, 1001, stat_line(106, "x", 106), status_ok=False)  # no status
    (fake_proc / "107").mkdir()
    (fake_proc / "107" / "status").write_text("Uid:\tnotanumber\n")
    (fake_proc / "self").mkdir()  # non-numeric entry ignored
    got = sorted(ou.SystemHostProbe().user_processes(1001))
    assert got == [(100, 100), (101, 100)]


def test_user_processes_missing_proc_returns_empty(monkeypatch):
    def boom(p):
        raise OSError("no proc")

    monkeypatch.setattr(ou.os, "listdir", boom)
    assert ou.SystemHostProbe().user_processes(1001) == []


# ---- sweep_stray argv safety --------------------------------------------------------------


async def test_sweep_stray_sids_are_clean_positive_ints_and_never_inject(make_config):
    sl, probe, _ = launcher(make_config)
    probe.oc_stats = [
        (1, "1 (x) S 1 2 5000 0"),
        (2, "2 (x) S 1 2 -v 0"),  # not an int
        (3, "3 (x) S 1 2 7;rm 0"),  # not an int
        (4, "4 (x) S 1 2 0 0"),  # session 0 must never be targeted
        (5, "5 (x) S 1 2 -5 0"),  # negative
        (6, "6 (x) S 1 2 6000,1 0"),  # comma smuggling
        (7, "7 (x) S 1 2 8000 0"),
    ]
    sl.register_serve(4000)
    await sl.sweep_stray()
    assert probe.pkills == [[PKILL, "-KILL", "-U", "brokeoc", "-s", "5000,8000"]]
    for a in probe.pkills[0][5:]:
        assert re.fullmatch(r"[1-9][0-9]*(,[1-9][0-9]*)*", a)


async def test_sweep_stray_targets_oc_user_only_never_daemon(make_config):
    sl, probe, _ = launcher(make_config)
    probe.oc_stats = [(5, stat_line(5, "sleep", 5000))]
    sl.register_serve(4000)
    await sl.sweep_stray()
    argv = probe.runs[0]
    assert argv[:5] == [ou.SUDO, "-n", "-u", "brokeoc", "--"]
    assert "justin" not in argv and "1000" not in argv and OC.name in argv
    assert argv[argv.index("-U") + 1] == "brokeoc"


async def test_sweep_stray_noop_when_user_unknown(make_config):
    sl, probe, _ = launcher(make_config)
    probe.users.pop("brokeoc")
    probe.oc_stats = [(5, stat_line(5, "sleep", 5000))]
    sl.register_serve(4000)
    await sl.sweep_stray()
    assert probe.runs == []


# ---- TASKS helper -------------------------------------------------------------------------


def test_tasks_oc_helper_is_one_valid_line():
    lines = (ROOT / "TASKS.md").read_text(encoding="utf-8").splitlines()
    hits = [i for i, ln in enumerate(lines) if "OC() {" in ln]
    assert len(hits) == 1
    ln = lines[hits[0]]
    assert ln.count("{") == ln.count("}") and ln.count("`") == 2
    assert r"""printf '%s\n' "$PW" """.strip() in ln
    assert ln.rstrip().endswith("}`")
    assert "$@" in ln


# ---- fake lifetime cap --------------------------------------------------------------------


def test_fake_spawn_child_heartbeat_loop_is_capped(tmp_path):
    import time

    hb = tmp_path / "hb"
    env = {
        **os.environ,
        "FAKE_OC_MAX_LIFETIME_S": "1",
        "FAKE_OC_HEARTBEAT": str(hb),
        "FAKE_OC_SCENARIOS": "spawn_child",
    }
    p = subprocess.Popen(
        [sys.executable, FAKE, "run", "--format", "json", "x"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while not hb.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert hb.exists()
        time.sleep(2.5)
        size = hb.stat().st_size
        time.sleep(0.6)
        assert hb.stat().st_size == size  # heartbeat child has stopped by itself
    finally:
        p.kill()
        p.wait()
