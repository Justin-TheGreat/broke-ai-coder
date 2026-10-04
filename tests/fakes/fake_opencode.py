"""Fake `opencode` CLI for offline tests (stdlib only). See tests/test_opencode_executor.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

SID = "ses_fake1"


def emit(obj: object) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def raw(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def session_event() -> None:
    emit({"type": "step_start", "sessionID": SID, "part": {"type": "step-start"}})


def success() -> None:
    session_event()
    emit({"type": "text", "sessionID": SID, "part": {"text": "done"}})
    emit(
        {
            "type": "tool_use",
            "sessionID": SID,
            "part": {
                "tool": "edit",
                "state": {"status": "completed", "input": {"filePath": "src/a.py"}},
            },
        }
    )
    emit(
        {
            "type": "step_finish",
            "sessionID": SID,
            "part": {"tokens": {"input": 10, "output": 5}},
        }
    )


def error_event(name: str, data: dict, with_session: bool = True) -> None:
    obj: dict = {"type": "error", "error": {"name": name, "data": data}}
    if with_session:
        obj["sessionID"] = SID
    emit(obj)


def pick_scenario() -> str:
    names = [n for n in os.environ.get("FAKE_OC_SCENARIOS", "success").split(",") if n]
    counter = os.environ.get("FAKE_OC_COUNTER_FILE")
    n = 0
    if counter:
        try:
            with open(counter) as f:
                n = int(f.read().strip() or "0")
        except FileNotFoundError:
            n = 0
        with open(counter, "w") as f:
            f.write(str(n + 1))
    return names[min(n, len(names) - 1)]


def main() -> int:
    argv_file = os.environ.get("FAKE_OC_ARGV_FILE")
    if argv_file:
        with open(argv_file, "a") as f:
            f.write(json.dumps(sys.argv[1:]) + "\n")
    cwd_file = os.environ.get("FAKE_OC_CWD_FILE")
    if cwd_file:
        with open(cwd_file, "a") as f:
            f.write(os.getcwd() + "\n")

    scenario = pick_scenario()
    if scenario == "success":
        success()
        return 0
    if scenario == "rate_limit":
        session_event()
        error_event(
            "APIError",
            {
                "message": "Too Many Requests",
                "statusCode": 429,
                "responseHeaders": {"retry-after": "30"},
            },
        )
        return 1
    if scenario == "quota_text":
        session_event()
        error_event("UnknownError", {"message": "Resource has been exhausted (e.g. check quota)."})
        return 1
    if scenario == "auth":
        error_event("ProviderAuthError", {"message": "bad key"}, with_session=False)
        return 1
    if scenario == "no_session_429":
        error_event("APIError", {"message": "slow down", "statusCode": 429}, with_session=False)
        return 1
    if scenario == "tool_no_session":
        emit({"type": "tool_use", "part": {"tool": "bash", "state": {"status": "running"}}})
        sys.stderr.write("boom\n")
        return 1
    if scenario == "exit_nonzero":
        sys.stderr.write("boom\n")
        return 3
    if scenario == "error_exit0":
        session_event()
        error_event("APIError", {"message": "unavailable", "statusCode": 503})
        return 0
    if scenario == "garbage":
        raw("this is not json")
        raw("[]")
        raw("{broken")
        success()
        return 0
    if scenario == "hang":
        session_event()
        time.sleep(120)
        return 0
    if scenario == "hang_no_session":
        time.sleep(120)
        return 0
    if scenario == "spawn_child":
        hb = os.environ["FAKE_OC_HEARTBEAT"]
        code = (
            f"import time\nwhile True:\n    open({hb!r}, 'a').write('x\\n')\n    time.sleep(0.1)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        session_event()
        time.sleep(120)
        return 0
    if scenario == "flood":
        chunk = "x" * 1000
        for _ in range(20_000):
            emit({"type": "text", "sessionID": SID, "part": {"text": chunk}})
        return 0
    if scenario == "long_line":
        sys.stdout.write("y" * (3 * 1024 * 1024) + "\n")
        sys.stdout.flush()
        success()
        return 0
    if scenario == "env_dump":
        name = os.environ.get("FAKE_OC_BOT_VAR", "")
        emit({"type": "text", "sessionID": SID, "part": {"text": f"present={name in os.environ}"}})
        return 0
    if scenario == "secret_echo":
        secret = os.environ.get("FAKE_OC_SECRET", "")
        emit({"type": "text", "sessionID": SID, "part": {"text": f"token is {secret}"}})
        return 0
    if scenario == "stderr_flood":
        chunk = "e" * 1000 + "\n"
        for _ in range(5_000):
            sys.stderr.write(chunk)
        sys.stderr.write("final-marker\n")
        sys.stderr.flush()
        return 2
    if scenario == "stderr_secret":
        secret = os.environ.get("FAKE_OC_SECRET", "")
        sys.stderr.write(f"auth failed with {secret}\n")
        return 2
    sys.stderr.write("unknown scenario\n")
    return 9


if __name__ == "__main__":
    raise SystemExit(main())
