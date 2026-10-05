"""Fake `opencode` CLI for offline tests (stdlib only). See tests/test_opencode_executor.py."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SID = "ses_fake1"


def max_lifetime_s() -> float:
    """Hard cap so a hard-killed test run cannot leave fakes running forever."""
    return float(os.environ.get("FAKE_OC_MAX_LIFETIME_S", "180"))


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


def serve(argv: list[str]) -> int:
    """`opencode serve` stand-in: parses --hostname/--port, serves health/config/abort."""
    host, port = "127.0.0.1", 4096
    for i, a in enumerate(argv):
        if a == "--hostname":
            host = argv[i + 1]
        elif a == "--port":
            port = int(argv[i + 1])
    scenario = os.environ.get("FAKE_OC_SERVE_SCENARIO", "ok")
    counter = os.environ.get("FAKE_OC_SERVE_COUNTER_FILE")
    n = 0
    if counter:
        try:
            with open(counter) as f:
                n = int(f.read().strip() or "0")
        except FileNotFoundError:
            n = 0
        with open(counter, "w") as f:
            f.write(str(n + 1))
    env_file = os.environ.get("FAKE_OC_ENV_FILE")
    if env_file:
        with open(env_file, "w") as f:
            json.dump(dict(os.environ), f)
    if scenario == "exit_immediately":
        sys.stderr.write("boom\n")
        sys.stderr.flush()
        return 5
    password = os.environ.get("OPENCODE_SERVER_PASSWORD")
    expected = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

        def _send(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            if password and self.headers.get("Authorization") != expected:
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._authed():
                return
            if self.path == "/global/health":
                if scenario == "never_healthy":
                    self._send(503, {"healthy": False})
                else:
                    self._send(200, {"healthy": True, "version": "fake"})
            elif self.path == "/config":
                override = os.environ.get("FAKE_OC_CONFIG_OVERRIDE")
                text = override or os.environ.get("OPENCODE_CONFIG_CONTENT", "{}")
                self._send(200, json.loads(text))
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authed():
                return
            if self.path.startswith("/session/") and self.path.endswith("/abort"):
                self._send(200, True)
            else:
                self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer((host, port), Handler)
    if scenario == "crash_after" and n == 0:
        delay = float(os.environ.get("FAKE_OC_SERVE_CRASH_AFTER_S", "1"))

        def crash() -> None:
            time.sleep(delay)
            os._exit(7)

        threading.Thread(target=crash, daemon=True).start()
    cap = max_lifetime_s()

    def expire() -> None:
        time.sleep(cap)
        os._exit(0)

    threading.Thread(target=expire, daemon=True).start()
    server.serve_forever()
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        return serve(sys.argv[2:])
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
            "import time\n"
            f"end = time.time() + {max_lifetime_s()!r}\n"
            "while time.time() < end:\n"
            f"    open({hb!r}, 'a').write('x\\n')\n"
            "    time.sleep(0.1)\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        session_event()
        time.sleep(min(120.0, max_lifetime_s()))
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
