# Spec: slice 3: OpenCode executor, orchestrator, Discord frontend, daemon wiring (offline-testable)

This slice covers these tasks:
- T022, T023 and T063: the OpenCode executor, session resume and cancel.
- T012, T020, T054 and T055: the orchestrator, fallback and unknown-outcome handling.
- T010, T011, T013 and T090-T092: the Discord frontend, kept minimal.
- CLI wiring, one doc fix, and the trivial security follow-ups.

Branch: `feat/discord-opencode`. The source docs are `SPEC.md`, `ARCHITECTURE.md` and `TASKS.md`. The project is FREE-ONLY. Do not add or reference any paid mode, budget or approval. `CostClass` has only `FREE`.

Conventions from slices 1 and 2 still apply. Copy them from the named files:
- Put `from __future__ import annotations` at the top of every module.
- Use frozen slotted dataclasses (`app/router/types.py`) and `StrEnum`.
- Pydantic models use `_CFG = ConfigDict(extra="forbid", frozen=True)` (`app/config/models.py`).
- Get loggers with `logging.getLogger(__name__)`. Do not use `print` outside `app/cli.py`.
- Repositories take a `sqlite3.Connection`, write inside `transaction(conn)` and use `to_db`/`from_db` (copy `app/db/tasks.py`).
- Classification never raises (copy `HttpProviderAdapter.classify_error`/`health_check` in `app/providers/http.py`).
- Logs carry only class names and ErrorClass values. Never log prompts, output, tokens or keys.

## OPEN QUESTIONS

None blocking. Every uncertain external detail is listed under Assumptions (§0.2) and lives as a constant or config default in one place. Implement them as written.

## 0. Environment, assumptions, decisions

### 0.1 Environment
- Use `.venv/Scripts/python` only (it is Python 3.14). Do not use the bare `python`.
- Add `"discord.py>=2.5,<3"` to `[project] dependencies` in `pyproject.toml`, then run `.venv/Scripts/python -m pip install -e ".[dev]"`. Installing needs the network. If discord.py does not install on 3.14, stop and report it. Do not vendor it or work around it.
- Gate: `.venv/Scripts/python -m pytest`, `-m ruff check .` and `-m ruff format --check .` (this is `scripts/check.ps1`). All 635 existing tests must stay green. The 2 strict xfails stay as they are.
- Tests must never contact Discord, run a real `opencode`, or use the network:
  - httpx goes through `MockTransport` (the conftest guard already blocks real transports).
  - The executor runs `tests/fakes/fake_opencode.py` through `sys.executable`.
  - No test calls `discord.Client.start/login/connect`.
- Never open or read `discord.txt` or `provider_api.txt` in the repo root. They are git-ignored secret files.

### 0.2 Assumptions (state each one in a module docstring or comment at the place given)

| # | Assumption | Where |
|---|---|---|
| B1 | `opencode run --format json` writes one JSON object per stdout line. Each object has `"type"` (str), and the types we use are `step_start`, `step_finish`, `text`, `tool_use` and `error`. Other types are ignored. The session id is the top-level `"sessionID"` (fallback: `part.sessionID`). | `app/opencode/events.py` |
| B2 | `text` events carry `part.text` (str). `tool_use` events carry `part.tool` (str), `part.state.status` (str) and, for tools `edit`/`write`/`patch`, `part.state.input.filePath` (str). `step_finish` events carry `part.tokens.input`/`part.tokens.output` (ints). | `events.py` |
| B3 | `error` events carry `error: {name: str, data: {message?: str, statusCode?: int, responseHeaders?: {str: str}, responseBody?: str}}`. The known names are `ProviderAuthError`, `APIError`, `MessageOutputLengthError`, `MessageAbortedError` and `UnknownError`. | `events.py` |
| B4 | OpenCode may retry 429s internally without emitting an event. The only bound on that is `opencode.run_timeout_s`. A timeout is an unknown outcome. | `executor.py` |
| B5 | OpenCode's model argument is `<opencode_provider>/<model>`. OpenCode splits on the **first** `/`, so `openrouter` + `openrouter/free` becomes `openrouter/openrouter/free`. | `executor.py` |
| B6 | The default provider map is `{"openrouter": "openrouter", "gemini": "google", "groq": "groq"}`. Gemini in AI Studio is the `google` provider in OpenCode (the setup doc uses `GOOGLE_GENERATIVE_AI_API_KEY` and `opencode models google`). It can be overridden with `opencode.provider_map`. An unmapped provider id is passed through unchanged. | `OpenCodeConfig` default |
| B7 | `GET {server_url}/global/health` returns `{"healthy": true, "version": "<str>"}`. The server runs without auth on localhost, so Basic auth (`OPENCODE_SERVER_PASSWORD`) is not supported in this slice. | `app/opencode/health.py` |
| B8 | With `--attach`, the process cwd selects the project directory, so the executor sets `cwd` to the project directory. | `executor.py` |
| B9 | `opencode run` reads piped stdin when stdin is not a TTY, so stdin is always `DEVNULL`. | `executor.py` |
| B10 | Each `step_finish` event is one LLM request. For quota estimates, an attempt counts `max(steps, 1)` requests. | orchestrator |
| B11 | `POST {server_url}/session/{id}/abort` stops the server-side agent loop of that OpenCode session. With `--attach` the loop runs in the server, so killing the CLI client is not enough. The executor sends it best effort (never raises, class-name-only logging, session id validated with `fullmatch` before use in the URL) on cancel, timeout and outer cancellation, once a session id is known. Gap: before the first event with a session id, nothing can be aborted. | `app/opencode/abort.py`, `executor.py` |

### 0.3 Decisions
1. **Empty Discord allowlists mean DENY ALL.** A command is authorized only when the user id is in `allowed_user_ids`, the guild id is in `allowed_guild_ids` and the channel id is in `allowed_channel_ids`. Any empty list denies everyone. DMs (guild None) are always denied. Auth runs before anything else, including the defer.
2. **One task runs at a time.** The existing single worker loop runs tasks one after another. Others wait in the bounded queue. This avoids concurrent edits to the same repo.
3. **The task row is created at submit time.** `/code` creates the QUEUED row synchronously, so it can return the task id in the ack. The worker loads the row by id.
4. **Fallback continues the session, never blindly replays:**
   - If the failed attempt captured an OpenCode session id, the next attempt runs `--session <id>` with `opencode.continue_prompt`. The original prompt is not re-sent.
   - If no session id was ever captured and the attempt saw zero `tool_use` events, the original prompt is re-sent without `--session`, because no side effect was observed.
   - If no session id was captured but `tool_use` events were seen, the task stops with error_class `UNKNOWN` (outcome unknown, no replay).
5. **Unknown outcomes stop the task:**
   - A timeout gives `TIMEOUT_UNKNOWN_OUTCOME`.
   - A non-zero exit with no classifiable error gives `UNKNOWN`.
   - Both are already in `STOP_ERRORS`, so the router returns `NON_FALLBACK_ERROR`.
   - The task fails with that error class, and the final message tells the user they can continue with `/code session:<id>`.
6. **Executor infrastructure failures are not provider failures.** These cases return outcome `EXECUTOR_UNAVAILABLE`:
   - the executable is missing;
   - the executable resolves to `.cmd`/`.bat`;
   - the working directory is missing;
   - spawning raises OSError;
   - the OpenCode health check fails before the first attempt.

   The task then fails with error_class `OPENCODE_UNAVAILABLE`. No cooldown is applied and no fallback happens.
7. **`.cmd`/`.bat` executables are refused on Windows.** Passing a user prompt through cmd.exe allows metacharacter injection. The user must set `opencode.command` to a real executable (for example `["node", "C:/path/opencode/bin/opencode"]`) or run in WSL. Production runs in WSL anyway.
8. **Prompt hygiene:**
   - Strip NUL characters and surrounding whitespace.
   - Reject an empty prompt or one longer than `PROMPT_MAX_CHARS = 4000`, with INVALID_REQUEST before routing.
   - If the prompt starts with `-`, prefix one space so yargs does not parse it as a flag.
   - The prompt is passed as a single argv element. No shell is used.
9. **Session ids from OpenCode are validated** against `^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$` before they are stored or passed back on argv. An invalid id is ignored.
10. **`tool_calling` is not required by default.** `orchestrator.require_tool_calling: false`, because the Gemini and Groq adapters cannot discover tool support without config metadata. The allowlist is the operator's control.
11. **Restart recovery is conservative.** On start:
    - every `ROUTING`/`RUNNING`/`WAITING_APPROVAL` task becomes `FAILED` with error_class `INTERRUPTED_BY_RESTART`;
    - every `QUEUED` task becomes `CANCELLED` with the same error_class;
    - nothing is replayed.
12. **Shutdown cancels the work:**
    - `controller.stop()` calls `orchestrator.begin_shutdown()`.
    - The running task's cancel event is set, its process tree is killed, and it ends `CANCELLED` with error_class `DAEMON_SHUTDOWN`.
    - Tasks still queued are marked `CANCELLED` (`DAEMON_SHUTDOWN`) without running.
13. **A missing Discord token does not crash the daemon.** If `discord.bot_token_env` does not resolve, the daemon starts with `NullFrontend` and logs at WARNING: `discord frontend disabled: env DISCORD_BOT_TOKEN is not set` (the env var **name**, never a value). `--once` always uses `NullFrontend`.
14. **Discord replies:**
    - Every reply passes through `SecretRedactor.redact`, then is truncated to `DISCORD_MAX_CHARS = 1900` with the suffix `\n…(truncated)`.
    - Triple backticks inside user or OpenCode text are neutralized (```` ``` ```` becomes `` `\u200b`` ``).
    - Messages are sent with `allowed_mentions=discord.AllowedMentions.none()`.
15. **Commands sync per guild** to each id in `allowed_guild_ids`, which is instant. Nothing is synced globally.
16. **Sessions:**
    - A new `/code` without `session` creates a `sessions` row (OPEN).
    - `/code session:<ref>` continues an OPEN session. Its first attempt uses `--session <opencode_session_id>` with the user's new prompt. If the session has no OpenCode id yet, the attempt runs without `--session`.
    - On a terminal task, the session stays OPEN when it has an `opencode_session_id`; otherwise it is closed.
    - Each cleanup run closes OPEN sessions that have been idle longer than `orchestrator.session_idle_close_days` and have no unfinished task.
    - The orchestrator calls `SessionRepository.touch` at each provider selection and at each terminal transition.
17. **Short refs:** `/cancel` and `/code session:` accept a full id or a unique prefix of ≥ 6 chars matching `^[0-9a-f-]{6,36}$`. Anything else is rejected before any SQL runs. An ambiguous prefix gets the reply "ambiguous id".

## 1. Files

Create:
```text
app/opencode/__init__.py          (only the future import)
app/opencode/events.py            pure JSON-line parsing + error classification
app/opencode/process.py           kill_process_tree
app/opencode/health.py            OpenCode server health check
app/opencode/executor.py          OpenCodeExecutor (AgentExecutor impl)
app/db/sessions.py                SessionRepository
app/db/attempts.py                FallbackAttemptRepository
app/orchestrator/orchestrator.py  Orchestrator
app/bot/__init__.py               (only the future import)
app/bot/auth.py                   authorize()
app/bot/format.py                 pure formatting
app/bot/commands.py               BotBackend protocol, snapshots, pure command handlers
app/bot/backend.py                ControllerBackend (BotBackend over controller/orchestrator)
app/bot/discord_frontend.py       thin discord.py adapter + create_frontend()
app/runtime/wiring.py             build/close the real component graph
tests/fakes/__init__.py           (empty)
tests/fakes/fake_opencode.py      fake CLI (§9.1)
tests/fakes/fake_executor.py      ScriptedExecutor (§9.2)
tests/test_opencode_events.py
tests/test_opencode_executor.py
tests/test_opencode_health.py
tests/test_sessions_attempts_repo.py
tests/test_orchestrator.py
tests/test_bot_auth.py
tests/test_bot_format.py
tests/test_bot_commands.py
tests/test_discord_frontend.py
tests/test_wiring.py
```
Modify:
```text
pyproject.toml                    add discord.py dependency
app/config/models.py              OpenCodeConfig fields, OrchestratorConfig, base_url http rule
app/config/secrets.py             resolve_secret strips
app/providers/http.py             truncate request_id
app/runtime/interfaces.py         ExecutionOutcome/ExecutionResult/ExecutorEvent/ExecutorHealth/AgentExecutor/ChatFrontend.on_event
app/runtime/daemon.py             TaskSubmission fields, enqueue_task, cancel_task, orchestrator hookup, on_event fan-out, idle-session close
app/db/tasks.py                   set_selection, attach_session, find_by_ref, list_recent
app/db/provider_models.py         list_model_ids
app/cli.py                        run real graph via wiring
config.example.yaml               new opencode/orchestrator keys (values = defaults)
docs/SETUP-discord-opencode.md    remove the feat/providers-quota checkout line
tests/test_config.py, tests/test_daemon.py   add tests (§10)
```
Keep the existing fixtures in `tests/conftest.py` intact. Add a helper there only if it is shared.

## 2. Config (`app/config/models.py`)

### 2.1 `OpenCodeConfig` (extend it; keep the existing two fields and their defaults)
```python
server_url: str = "http://127.0.0.1:4096"  # validator: must start with http:// or https://
working_directory: str = "/workspace"
command: list[str] = Field(
    default_factory=lambda: ["opencode"], min_length=1
)  # entries stripped, non-empty
provider_map: dict[str, str] = Field(
    default_factory=lambda: {"openrouter": "openrouter", "gemini": "google", "groq": "groq"}
)  # keys/values stripped, non-empty, values must not contain "/"
projects: dict[str, str] = Field(
    default_factory=dict
)  # name -> directory; name matches ^[A-Za-z0-9_.-]{1,64}$; "default" is reserved (rejected as a key)
run_timeout_s: float = Field(1800, gt=0, le=86400, allow_inf_nan=False)
kill_grace_s: float = Field(5, gt=0, le=60, allow_inf_nan=False)
health_timeout_s: float = Field(5, gt=0, le=60, allow_inf_nan=False)
max_line_bytes: int = Field(1_048_576, ge=1024, le=16_777_216)
output_tail_chars: int = Field(4000, ge=200, le=20000)
stderr_tail_bytes: int = Field(4096, ge=256, le=65536)
continue_prompt: str = "Continue the previous task from where it stopped. Do not repeat steps that already completed."  # stripped, non-empty
```
Add `def project_directory(self, project: str) -> str | None`. It returns `working_directory` for `"default"`, `projects.get(project)` otherwise, and None when the project is unknown.

### 2.2 New `OrchestratorConfig`, added as `AppConfig.orchestrator` (default_factory)
```python
provider_refresh_interval_s: float = Field(
    300, ge=0, allow_inf_nan=False
)  # 0 = refresh before every task
progress_min_interval_s: float = Field(10, ge=0, allow_inf_nan=False)
session_idle_close_days: float = Field(7, gt=0, allow_inf_nan=False)
require_tool_calling: bool = False
```

### 2.3 Trivial review follow-ups
- `ProviderConfig._base_url_valid`: keep the https/http prefix check. Additionally, reject `http://` unless `urllib.parse.urlsplit(v).hostname` is in `{"localhost", "127.0.0.1", "::1"}`. The message is `base_url must use https:// (http:// only for localhost)`.
- `app/config/secrets.py` `resolve_secret`: return `value.strip()`. A whitespace-only value is still None.
- `app/providers/http.py` `error_from_response`: truncate `request_id` to 100 chars (`request_id[:100] if request_id else None`).

### 2.4 `config.example.yaml`
- Add the new `opencode` keys and an `orchestrator:` section. Show the default values, with commented `projects` and `provider_map` examples.
- Comment that empty Discord allowlists deny everyone.
- `tests/test_cli.py::test_check_example_config` must still pass.

## 3. Runtime interfaces (`app/runtime/interfaces.py`)

Replace `ExecutionResult` and `AgentExecutor`, and extend `ChatFrontend`:
```python
class ExecutionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"


class ExecutorEventKind(StrEnum):
    SESSION = "SESSION"  # session id first seen
    TOOL = "TOOL"  # tool_use event
    STEP = "STEP"  # step_finish


@dataclass(frozen=True, slots=True)
class ExecutorEvent:
    kind: ExecutorEventKind
    session_id: str | None = None
    tool: str | None = None  # truncated to 40 chars
    tool_status: str | None = None  # truncated to 20 chars


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    exit_code: int | None = None
    error_class: ErrorClass | None = None  # None for SUCCEEDED/CANCELLED/EXECUTOR_UNAVAILABLE
    session_id: str | None = None
    http_status: int | None = None
    retry_after_s: float | None = None
    error_message: str | None = None  # redacted, <= 300 chars
    output_tail: str = ""  # redacted, <= output_tail_chars
    output_truncated: bool = False
    tool_events: int = 0
    llm_steps: int = 0
    input_tokens: int | None = None  # None unless every step_finish carried ints
    output_tokens: int | None = None
    changed_files: tuple[str, ...] = ()  # <= 20 unique, each <= 200 chars, first-seen order
    detail: str | None = None  # short infra detail for EXECUTOR_UNAVAILABLE, no secrets


@dataclass(frozen=True, slots=True)
class ExecutorHealth:
    healthy: bool
    version: str | None
    detail: str


class AgentExecutor(Protocol):
    async def health_check(self) -> ExecutorHealth: ...
    async def run(
        self,
        *,
        task_id: str,
        provider: str,
        model: str,
        prompt: str,
        session_id: str | None,
        project_dir: str,
        cancel_event: asyncio.Event,
        on_event: Callable[[ExecutorEvent], None] | None = None,
    ) -> ExecutionResult: ...
    async def aclose(self) -> None: ...


class ChatFrontend(Protocol):
    async def start(self, controller: AgentController) -> None: ...
    async def stop(self) -> None: ...
    async def on_event(self, event: ControllerEvent) -> None: ...
```
`NullFrontend.on_event` returns None. Import `ControllerEvent` under `TYPE_CHECKING` only.

## 4. OpenCode package

### 4.1 `app/opencode/events.py` (pure, never raises)
```python
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,127}$")
FILE_TOOLS = frozenset({"edit", "write", "patch"})

@dataclass(frozen=True, slots=True)
class OpenCodeError:
    error_class: ErrorClass
    http_status: int | None
    retry_after_s: float | None
    message: str            # unredacted here, <= 300 chars; executor redacts

@dataclass(frozen=True, slots=True)
class ParsedEvent:
    type: str
    session_id: str | None = None
    text: str | None = None
    tool: str | None = None
    tool_status: str | None = None
    file_path: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: OpenCodeError | None = None

def parse_event_line(line: str | bytes, *, now: datetime) -> ParsedEvent | None
def classify_opencode_error(error: object, *, now: datetime) -> OpenCodeError
def classify_text(text: str) -> ErrorClass   # used for no-status errors and stderr
```
`parse_event_line` returns None in these cases:
- the line is invalid UTF-8 (decode with `errors="replace"`, then attempt JSON) or not JSON;
- the JSON is not a dict;
- `type` is missing or not a str.

Read every field by `isinstance` checks. Token fields count only when they are a real int ≥ 0 (not a bool). Session ids must match `SESSION_ID_RE`.

`classify_opencode_error`:
1. The input is not a dict → `UNKNOWN`. Read `name` and `data` (`data` must be a dict, otherwise use `{}`). Build `text = message + " " + responseBody`, each part only if it is a str, and cap it at 2000 chars.
2. `name == "ProviderAuthError"` → `AUTH_FAILED`.
3. `name == "MessageOutputLengthError"` → `CONTEXT_TOO_LARGE`.
4. `statusCode` is an int in 100..599. Start with `app.providers.http.classify_status(status)`, then refine:
   - 429: if `classify_text` gives `QUOTA_EXHAUSTED`, return that, else `RATE_LIMITED`.
   - 400 or 413: if the text says context, return `CONTEXT_TOO_LARGE`.
   - Never refine any other status. A 504 always stays `TIMEOUT_UNKNOWN_OUTCOME`.
5. Otherwise, use `classify_text(text)`.

`retry_after_s`: look up `"retry-after"` in `data.responseHeaders`, case-insensitively, and parse it with `app.providers.ratelimit.parse_retry_after(value, now)`.

`classify_text`: use case-insensitive regexes, checked in this order:
| Order | Pattern | Class |
|---|---|---|
| 1 | `quota\|resource.?exhausted\|insufficient_quota` | `QUOTA_EXHAUSTED` |
| 2 | `rate.?limit\|too many requests\|\b429\b` | `RATE_LIMITED` |
| 3 | `context.?(length\|window)\|maximum context\|too many tokens` | `CONTEXT_TOO_LARGE` |
| 4 | `model.{0,40}not.?found\|model_not_found\|unknown model` | `MODEL_UNAVAILABLE` |
| 5 | none of the above | `UNKNOWN` |

### 4.2 `app/opencode/process.py`
```python
async def kill_process_tree(proc: asyncio.subprocess.Process, *, grace_s: float) -> None
```
It never raises, and it returns once `proc.returncode is not None` or after the best effort below.
- **POSIX:** the process was started with `start_new_session=True`.
  1. `os.killpg(proc.pid, SIGTERM)`.
  2. Wait up to `grace_s` for `proc.wait()`.
  3. Then `os.killpg(proc.pid, SIGKILL)`.
  4. Ignore `ProcessLookupError`/`PermissionError`.
- **Windows:**
  1. Run `taskkill /PID <pid> /T /F` through `asyncio.create_subprocess_exec` with stdout/stderr DEVNULL, and wait up to 10 s.
  2. Then, if the process is still alive, `proc.kill()`.
  3. Then wait up to `grace_s`.

  The process was started with `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW`.
- If the process has already exited (`returncode` is set), skip killing the root process, but on POSIX still `killpg` to reap any orphans (ignore errors).
- Accepted limitation (document it in a comment): on Windows, grandchildren of an already-exited root cannot be found through `/T`.

### 4.3 `app/opencode/health.py`
```python
async def check_opencode_health(server_url: str, *, timeout_s: float,
                                transport: httpx.AsyncBaseTransport | None = None) -> ExecutorHealth
```
- Make one `httpx.AsyncClient(transport=transport, timeout=timeout_s, follow_redirects=False)` per call, used with `async with`.
- GET `server_url.rstrip("/") + "/global/health"`.
- It is healthy only for a 200 response whose JSON is a dict with `healthy is True`. Take `version` only if it is a str, truncated to 64.
- Otherwise it is unhealthy, with detail `"HTTP <code>"`, `"malformed response"`, `"unhealthy"` or `type(e).__name__`.
- It never raises.

### 4.4 `app/opencode/executor.py`
```python
class OpenCodeExecutor:
    def __init__(self, config: OpenCodeConfig, *, redactor: SecretRedactor | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 extra_env: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None
    def opencode_model(self, provider: str, model: str) -> str   # f"{provider_map.get(provider, provider)}/{model}"
    def build_argv(self, executable: str, *, provider: str, model: str, prompt: str, session_id: str | None) -> list[str]
    async def health_check(self) -> ExecutorHealth
    async def run(...) -> ExecutionResult          # signature per §3
    async def aclose(self) -> None                  # no-op (no persistent resources)
```

`build_argv` returns `[executable, *command[1:], "run", "--attach", server_url, "--model", opencode_model, "--format", "json", ("--session", sid)?, prompt_arg]`. Here `prompt_arg` is the sanitized prompt, with a leading space added if it starts with `-`.

`run` steps:
1. **Sanitize the prompt** with `sanitize_prompt` (§6, Decision 8). If it is invalid, return `FAILED`/`INVALID_REQUEST` without spawning.
2. **Check the session id.** If `session_id` is not None and does not match `SESSION_ID_RE`, return `FAILED`/`INVALID_REQUEST`.
3. **Resolve the executable** with `shutil.which(command[0])`. Return `EXECUTOR_UNAVAILABLE` in these cases:
   - it resolves to None, with detail `"opencode executable not found"`;
   - on Windows, it resolves to a `.cmd`/`.bat` suffix (case-insensitive), with detail `"refusing .cmd/.bat launcher; set opencode.command"`;
   - `project_dir` is not an existing directory, with detail `"project directory missing"`.
4. **Spawn** with `asyncio.create_subprocess_exec(*argv, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, cwd=project_dir, env={**os.environ, **extra_env}, limit=max_line_bytes, **platform kwargs)`. If this raises OSError, return `EXECUTOR_UNAVAILABLE` with detail `type(e).__name__`.
5. **Read concurrently:**
   - **stdout reader:** loop `await stdout.readline()`.
     - A `ValueError` (oversized line) increments `oversized_lines` and continues. CPython drops the buffered overrun.
     - `b""` means EOF.
     - Parse each line with `parse_event_line`.
     - Track the first valid session id, and call `on_event(SESSION)` once.
     - Text appends to a bounded tail. Keep at most `output_tail_chars` chars, dropping from the front, and set `output_truncated` when anything is dropped.
     - Every `tool_use` increments `tool_events` and calls `on_event(TOOL)`. File tools add their path to `changed_files`.
     - `step_finish` increments `llm_steps`, sums tokens, and calls `on_event(STEP)`. If any step lacks ints, the token totals become None.
     - Keep the **first** error event.
     - `on_event` exceptions are caught and logged (class name only).
   - **stderr reader:** keep only the last `stderr_tail_bytes` bytes (a bounded `bytearray`, trimmed from the front).
   - Then `proc.wait()`.

   Memory must not grow with output size.
6. **Wait** with `asyncio.wait({drain_and_wait, cancel_event.wait()}, timeout=run_timeout_s, return_when=FIRST_COMPLETED)`:
   - On cancel → `kill_process_tree`, then outcome `CANCELLED`.
   - On timeout → kill, then outcome `TIMED_OUT`, `error_class=TIMEOUT_UNKNOWN_OUTCOME`.
   - After a kill, wait for the drain task for at most `kill_grace_s + 5` s, then cancel it.
   - Always cancel the helper `cancel_event.wait()` task.
7. **Raw `asyncio.CancelledError`** (shutdown): `await asyncio.shield(kill_process_tree(...))`, then re-raise.
8. **Map the result after a normal exit:**
   - First error event present → `FAILED` with its class, status, retry_after and message (even when the exit code is 0).
   - Exit 0 → `SUCCEEDED`.
   - Non-zero exit → `FAILED` with `classify_text(stderr_tail)`, and the message is the redacted stderr tail (≤ 300 chars).
9. **Redact before returning.** `output_tail`, `error_message` and `changed_files` go through `redactor.redact` when a redactor is set, after ANSI escapes are stripped (`\x1b\[[0-9;?]*[A-Za-z]`).
10. **Log** one INFO line: `task_id`, provider, model, outcome, exit_code, error_class, `llm_steps`, `tool_events`, `oversized_lines`. Never log the prompt, the argv, or any output.

## 5. DB repositories

### 5.1 `app/db/sessions.py`
```python
class SessionStatus(StrEnum): OPEN = "OPEN"; CLOSED = "CLOSED"

@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str; opencode_session_id: str | None; project_id: str; status: SessionStatus
    created_at: datetime; updated_at: datetime; closed_at: datetime | None

class AmbiguousReference(LookupError): ...

class SessionRepository:
    def create(self, *, project_id: str, now: datetime) -> SessionRecord
    def get(self, session_id: str) -> SessionRecord | None
    def find_by_ref(self, ref: str) -> SessionRecord | None   # exact id, else unique prefix (LIKE ref || '%' LIMIT 2); raises AmbiguousReference; caller validates ref format
    def set_opencode_session_id(self, session_id: str, oc_id: str, *, now: datetime) -> SessionRecord  # only when currently NULL; otherwise unchanged (log WARNING if a different id is offered)
    def touch(self, session_id: str, *, now: datetime) -> None
    def close(self, session_id: str, *, now: datetime) -> None      # idempotent
    def list_recent(self, limit: int) -> list[SessionRecord]        # ORDER BY updated_at DESC, id
    def close_idle(self, *, cutoff: datetime, now: datetime) -> int # OPEN, updated_at < cutoff, no task in non-terminal status
```
`app/db/sessions.py` must not import `app/db/tasks.py`, to avoid an import cycle.

### 5.2 `app/db/attempts.py`
```python
class AttemptStatus(StrEnum): RUNNING, SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, EXECUTOR_UNAVAILABLE

@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: int; task_id: str; session_id: str | None; attempt_no: int; provider: str; model: str
    status: AttemptStatus; error_class: str | None; http_status: int | None
    retry_after_ms: int | None; started_at: datetime; finished_at: datetime | None

class FallbackAttemptRepository:
    def start(self, *, task_id: str, session_id: str | None, attempt_no: int, provider: str, model: str, now: datetime) -> int
    def finish(self, attempt_id: int, *, status: AttemptStatus, error_class: str | None, http_status: int | None, retry_after_ms: int | None, now: datetime) -> None
    def list_for_task(self, task_id: str) -> list[AttemptRecord]   # ORDER BY attempt_no
```
- `session_id` here is our `sessions.id`, not the OpenCode id.
- `retry_after_ms` is clamped to `[0, 2**31-1]`, and None stays None.
- `quota_snapshot_id` stays NULL.

### 5.3 `app/db/tasks.py` additions
```python
def set_selection(self, task_id: str, *, provider: str, model: str, now: datetime) -> TaskRecord  # sets selected_provider/model, cost_class='FREE', last_event_at
def attach_session(self, task_id: str, session_id: str, *, now: datetime) -> TaskRecord
def find_by_ref(self, ref: str) -> TaskRecord | None   # same semantics as sessions; raises AmbiguousReference (import from app.db.sessions)
def list_recent(self, limit: int) -> list[TaskRecord]  # ORDER BY created_at DESC, id
```

### 5.4 `app/db/provider_models.py`
Add `list_model_ids(self, provider: str) -> list[str]`. It returns all rows for the provider (ALLOWED and DISCOVERED_ONLY), sorted.

## 6. Orchestrator (`app/orchestrator/orchestrator.py`)

```python
FAIL_OPENCODE_UNAVAILABLE = "OPENCODE_UNAVAILABLE"
FAIL_INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"
FAIL_DAEMON_SHUTDOWN = "DAEMON_SHUTDOWN"
PROMPT_MAX_CHARS = 4000

def sanitize_prompt(prompt: str) -> str | None   # Decision 8 minus the leading-space rule; None = invalid. Used by executor, orchestrator, backend.

@dataclass(frozen=True, slots=True)
class RunningTask:
    task_id: str; session_id: str; provider: str | None; model: str | None
    attempt_no: int; opencode_session_id: str | None; started_at: datetime

class CancelResult(StrEnum):
    CANCELLED = "CANCELLED"            # was QUEUED, now CANCELLED
    CANCELLING = "CANCELLING"          # running; cancel signalled
    NOT_FOUND = "NOT_FOUND"
    NOT_CANCELLABLE = "NOT_CANCELLABLE"   # already terminal

class Orchestrator:
    def __init__(self, config: AppConfig, *, executor: AgentExecutor,
                 adapters: Mapping[str, ProviderAdapter],
                 cooldowns: CooldownManager | None = None,
                 environ: Mapping[str, str] | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None
    def bind(self, controller: AgentController) -> None          # called by controller.start after migrate
    def recover_interrupted(self) -> int                          # Decision 11; returns count
    async def handle(self, controller: AgentController, sub: TaskSubmission) -> None   # the TaskHandler
    def request_cancel(self, task_id: str) -> CancelResult
    def begin_shutdown(self) -> None                              # Decision 12
    @property
    def running(self) -> RunningTask | None
    @property
    def executor(self) -> AgentExecutor
    @property
    def cooldowns(self) -> CooldownManager
    @property
    def observations(self) -> Mapping[str, ProviderObservation]
```
`sanitize_prompt` may instead live in a small `app/orchestrator/prompt.py` if that avoids an import cycle with `app/opencode/executor.py`. Pick one place.

### 6.1 `handle` algorithm
All DB access uses `controller.conn`, and timestamps use `self._clock()`. Steps:
1. **Load the task.** If `sub.task_id` is None, create the row as `_default_handler` does now; otherwise load it.
   - Missing → log a warning and return.
   - Status is not QUEUED (for example cancelled while queued) → return.
   - Shutting down → transition QUEUED → CANCELLED with `DAEMON_SHUTDOWN`, emit `task_finished`, and return.
2. **Register the cancel event.** Register `asyncio.Event` as the current cancel event and set `running`. Clear both in a `finally`.
3. **Start routing.** Transition QUEUED → ROUTING and emit `task_routing`.
4. **Validate the prompt and project:**
   - `project_dir = config.opencode.project_directory(record.project_id)`.
   - Unknown project, or `sanitize_prompt` returns None → transition ROUTING → FAILED with error_class `INVALID_REQUEST`, emit `task_finished`, and return.
5. **Resolve the session:**
   - If `record.session_id` is set, load it. If it is missing or CLOSED, transition ROUTING → FAILED with `INVALID_REQUEST`, emit, and return.
   - Otherwise `SessionRepository.create(project_id=...)`, then `TaskRepository.attach_session`.
   - `oc_sid = session.opencode_session_id`.
6. **Health check.** `health = await executor.health_check()`. If it is unhealthy → ROUTING → FAILED with `OPENCODE_UNAVAILABLE`, emit with `detail`, and return.
7. **Refresh providers.** Run `refresh_all(adapters, conn, config, now=..., environ=...)` when it has never run or `provider_refresh_interval_s` has elapsed. On any exception, log the class name and keep the previous observations.
8. **Build the request.** `request = RouteRequest(task_id, project_id, RequiredCapabilities(tool_calling=config.orchestrator.require_tool_calling), session_id=session.id)`. `state_factory = lambda: build_router_state(config, conn, cooldowns, observations, now=clock(), environ=environ)`.
9. **Loop.** Set `attempts: list[FailedAttempt] = []`, `prompt_to_send = sanitized prompt` and `send_session = oc_sid`, then repeat:
   1. If cancel is set → transition the task's current status (ROUTING) to CANCELLED (error_class `DAEMON_SHUTDOWN` when shutting down, else None), emit, and return.
   2. Route with `decision = route_with_state(request, config, state_factory, attempts=attempts)`.
   3. **NoEligibleProvider** → transition ROUTING → FAILED with this error_class:
      - `NON_FALLBACK_ERROR` → the last attempt's error_class value;
      - any other reason → `reason.value` (for example `FREE_CAPACITY_EXHAUSTED`).

      Emit `task_finished` with `reason`, `error_class` and `attempts=len(attempts)`, then return.
   4. **Selected:**
      - `set_selection`, `SessionRepository.touch`, then transition ROUTING → RUNNING.
      - `attempt_no = len(attempts) + 1`. Insert the attempt row with `start`.
      - Update `running`.
      - Emit `provider_selected` with provider, model, attempt_no, `policy_id`, and `skipped` (a comma-joined `provider/model:REASON` list, ≤ 300 chars).
   5. **Run** `result = await executor.run(task_id=..., provider=..., model=..., prompt=prompt_to_send, session_id=send_session, project_dir=project_dir, cancel_event=..., on_event=<throttled progress callback, §6.2>)`.
   6. **Persist the session id.** If `result.session_id` is set and the session has no OpenCode id → `set_opencode_session_id`, and set `oc_sid`.
   7. **Record usage:** `max(result.llm_steps, 1)` `UsageEvent`s (FREE, task_id, provider, model).
      - The first event carries `input_tokens`/`output_tokens`. The rest carry `0/0` when the totals are known, else None.
      - The status of the **last** event is `success` for SUCCEEDED, `unknown_outcome` for TIMED_OUT or for a FAILED attempt whose class is in `STOP_ERRORS`, and `error` otherwise. Earlier events are `success`.
      - Cap the count at 500 events (log if capped).
      - Skip this whole step for `EXECUTOR_UNAVAILABLE` and for a CANCELLED attempt with `llm_steps == 0`.
   8. **Finish the attempt row** (status from the outcome, `error_class`, `http_status`, `retry_after_ms` from `round(retry_after_s*1000)`), then handle the outcome:
      - **SUCCEEDED:** `cooldowns.record_success(provider)`. Transition RUNNING → SUCCEEDED with `exit_code`. Emit `task_finished` with these fields: `status`, `provider`, `model`, `attempts`, `changed_files` (comma-joined, ≤ 500 chars), `output_tail` (≤ 1500 chars), `session_id`, `exit_code`. Return.
      - **CANCELLED:** transition RUNNING → CANCELLED (`DAEMON_SHUTDOWN` if shutting down) and emit. Return.
      - **EXECUTOR_UNAVAILABLE:** RUNNING → FAILED with `OPENCODE_UNAVAILABLE`. Emit with `detail`. Return. There is no cooldown.
      - **FAILED / TIMED_OUT:**
        1. `ec = result.error_class or ErrorClass.UNKNOWN`.
        2. `cooldowns.record_failure(provider, model, ec, retry_after_s=result.retry_after_s, now=clock())`.
        3. Apply Decision 4. If `result.session_id is None and oc_sid is None and result.tool_events > 0`, set `ec = ErrorClass.UNKNOWN`.
        4. Append `FailedAttempt(provider, model, ec)`.
        5. Emit `attempt_failed` with provider, model, attempt_no, `error_class`, `http_status` and `retry_after_ms`.
        6. Transition RUNNING → ROUTING.
        7. Set the next send: if `oc_sid` is set → `prompt_to_send = config.opencode.continue_prompt`, `send_session = oc_sid`. Otherwise keep the original prompt and `send_session = None`.
        8. Continue the loop.
10. **`finally` block:**
    - Unregister the cancel event and `running`.
    - If the task is now terminal: `SessionRepository.touch`, and close the session if it has no `opencode_session_id` (Decision 16).
11. **Raw `CancelledError`:** best effort, transition the task to CANCELLED with `DAEMON_SHUTDOWN` (ignore `InvalidTransition`/`ConcurrentTransition`/`sqlite3.Error`), then re-raise.

Every transition uses `TaskRepository.transition`, so `ensure_transition` is enforced. The paths above are QUEUED → ROUTING → (RUNNING ⇄ ROUTING)* → terminal, and every one of them is legal in `ALLOWED_TRANSITIONS`.

### 6.2 Events (bounded, for the frontend)
Emit through `controller.emit(ControllerEvent(name, at, task_id, fields))`. Every task event's fields include `channel_id` (`record.discord_channel_id` or None) and `short_id` (the first 8 chars of the task id). The names are:
- `task_queued` (from `enqueue_task`)
- `task_routing`
- `provider_selected`
- `progress`
- `attempt_failed`
- `task_finished`

`task_finished` always has `status` (the TaskStatus value) and `error_class` (or None). It also has `session_short` (the first 8 chars of our session id, or None).

`progress` comes from executor events:
- **SESSION:** always emitted, with `opencode_session_id`.
- **TOOL:** emitted at most once per `progress_min_interval_s` per task, with `tool`, `tool_status`, `tool_events`, `llm_steps`.
- **STEP:** emitted at most once per `progress_min_interval_s` per task, with the same fields.
- Throttled events are dropped, not queued.

### 6.3 `request_cancel(task_id)`
1. The task is not found → `NOT_FOUND`.
2. The task is terminal → `NOT_CANCELLABLE`.
3. The task is the running one → set its event and return `CANCELLING`.
4. The task is QUEUED → transition it to CANCELLED, emit `task_finished`, and return `CANCELLED`. The worker skips it later.
5. Otherwise (ROUTING/RUNNING/WAITING_APPROVAL but not the tracked task, which should not happen) → `NOT_CANCELLABLE`, and log a warning.

## 7. Daemon changes (`app/runtime/daemon.py`)
- **`TaskSubmission`:** add `task_id: str | None = None` and `session_id: str | None = None`.
- **`AgentController.__init__`:**
  - Add `orchestrator: Orchestrator | None = None`. If it is given and `task_handler` is None, use `orchestrator.handle`.
  - Expose the read-only property `orchestrator`.
  - Keep the existing behavior when it is not given, and keep all current tests passing.
- **`start()`:** after `migrate`, if the orchestrator is set, call `orchestrator.bind(self)`, then `orchestrator.recover_interrupted()`. Both run before the worker and frontend start.
- **`enqueue_task(sub: TaskSubmission) -> TaskRecord | None`:**
  - Return None if the controller is not started, is stopped, or `self._task_queue.full()`.
  - Otherwise create the QUEUED row (with `session_id`), `put_nowait(dataclasses.replace(sub, task_id=record.id))`, emit `task_queued`, and return the record.
  - Do not `await` between the full-check and the put.
- **`cancel_task(task_id) -> CancelResult`:** delegate to the orchestrator. Without an orchestrator, cancel a QUEUED row directly, and otherwise return `NOT_CANCELLABLE`/`NOT_FOUND`.
- **`stop()`:** call `orchestrator.begin_shutdown()` (if set) **before** `frontend.stop()` and the queue drain.
- **`_event_loop`:** after logging, `await asyncio.wait_for(self._frontend.on_event(event), timeout=FRONTEND_EVENT_TIMEOUT_S)` with `FRONTEND_EVENT_TIMEOUT_S = 10`. Catch `Exception` (this includes `TimeoutError`) and log only the type name. A frontend failure must never kill the loop.
- **`run_cleanup_now`:** in `_work`, before retention, run `SessionRepository(conn).close_idle(cutoff=now - timedelta(days=config.orchestrator.session_idle_close_days), now=now)` and add `sessions_closed` to the event fields.
- **`metrics()`:** add `running_task_id` (or None).

## 8. Discord frontend

### 8.1 `app/bot/auth.py`
```python
@dataclass(frozen=True, slots=True)
class AuthDecision:
    allowed: bool
    reason: str     # "ok" | "user_not_allowed" | "guild_not_allowed" | "channel_not_allowed" | "dm_not_allowed"

def authorize(cfg: DiscordConfig, *, user_id: int, guild_id: int | None, channel_id: int | None) -> AuthDecision
```
Checks run in this order: user, then dm (guild None), then guild, then channel. A None channel is not allowed. Empty lists deny (Decision 1). Pure, with no logging.

### 8.2 `app/bot/format.py` (pure)
```python
DISCORD_MAX_CHARS = 1900
def safe_text(text: str, redactor: SecretRedactor | None, limit: int = DISCORD_MAX_CHARS) -> str  # strip ANSI, redact, neutralize ```, truncate with "\n…(truncated)"
def format_ack(record: TaskRecord, queue_position: int) -> str
def format_event(event: ControllerEvent) -> str | None       # None = do not post
def format_status(snapshot: StatusSnapshot) -> str
def format_usage(snapshot: UsageSnapshot) -> str
def format_models(groups: Sequence[ProviderModels]) -> str
def format_sessions(sessions: Sequence[SessionRecord]) -> str
def format_cancel(result: CancelResult, ref: str) -> str
```
Every function's return value goes through `safe_text` (callers pass the redactor, so add a `redactor` keyword to each function). The contents are:
- **Ack:** `Task <short_id> queued (position N). Project: <p>.`
- **`provider_selected`:** `Provider: <provider> / <model> (attempt n)`.
- **`attempt_failed`:** `Attempt n failed: <ERROR_CLASS> — trying next free candidate` (do not promise a next one).
- **`progress`:** `Working… <tool> (<tool_status>), steps <n>`. For a SESSION event: `OpenCode session started`.
- **`task_finished`:**
  - SUCCEEDED: `✅ Task <short> completed`, a `Changed:` list (if any), `Provider: p / m`, `Cost class: FREE`, and the output tail in a code block.
  - FAILED: `❌ Task <short> failed: <error_class>`.
    - For `FREE_CAPACITY_EXHAUSTED`, add the explicit line: `No free capacity left on any configured provider. Nothing was paid for. Try again after cooldowns/quotas reset (/usage).`
    - For `TIMEOUT_UNKNOWN_OUTCOME`/`UNKNOWN`, add: `Outcome unknown; not replayed. Continue with /code session:<session_short>`.
    - For `OPENCODE_UNAVAILABLE`, add: `OpenCode server is not reachable.`
  - CANCELLED: `🛑 Task <short> cancelled`.
- **`task_routing`/`task_queued`:** None.
- **Status:**
  - `PC: online`;
  - `OpenCode: healthy (<version>)` or `unreachable (<detail>)`;
  - the current task (short id, provider/model, attempt, OpenCode session short), or `Current task: none`;
  - `Queue: N`;
  - up to 5 recent tasks (`short status provider/model`).
- **Usage:** one line per provider/model/window:
  - `<provider>[/<model>] <window> <unit>: <remaining> remaining (EXACT)`;
  - `~<remaining> … (ESTIMATED)`;
  - `UNKNOWN`, with no number, when `remaining` is None or the confidence is UNKNOWN.

  Then the active cooldowns (`provider[/model] cooling until HH:MM UTC`), then today's observed usage (requests, tokens). With no data at all: `No quota data (UNKNOWN)`.
- **Models:** per policy in `provider_order`, numbered allowlisted models with `ALLOWED` and a capability summary (`tools`, `ctx=<n>`, or `caps unknown`). Then `N other models DISCOVERED_ONLY (never selectable)` when N > 0.
- **Sessions:** one line each: `<short id> <project> <status> oc=<first 12 chars of opencode id or ->`. With none: `No sessions.`

### 8.3 `app/bot/commands.py` (pure handlers, no discord import)
```python
@dataclass(frozen=True, slots=True)
class CommandContext: user_id: int; guild_id: int | None; channel_id: int | None

@dataclass(frozen=True, slots=True)
class Reply: content: str; ephemeral: bool

@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    opencode: ExecutorHealth; running: RunningTask | None; queue_depth: int
    recent: tuple[TaskRecord, ...]           # <= 5

@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    quota: tuple[QuotaRecord, ...]; cooldowns: Mapping[tuple[str, str | None], datetime]
    today: tuple[tuple[str, str, UsageTotals], ...]; now: datetime

@dataclass(frozen=True, slots=True)
class ProviderModels:
    policy_id: str; provider: str; listings: tuple[ModelListing, ...]
    capabilities: Mapping[str, ModelCapability]

class SubmitStatus(StrEnum): QUEUED, QUEUE_FULL, INVALID_PROMPT, UNKNOWN_PROJECT, UNKNOWN_SESSION, AMBIGUOUS_SESSION, SESSION_CLOSED

@dataclass(frozen=True, slots=True)
class SubmitResult: status: SubmitStatus; record: TaskRecord | None = None; queue_position: int = 0

class BotBackend(Protocol):
    def submit_code(self, *, prompt: str, project: str | None, session_ref: str | None, ctx: CommandContext) -> SubmitResult
    def cancel(self, task_ref: str) -> tuple[CancelResult | None, str]   # None + message for unknown/ambiguous ref
    async def status(self) -> StatusSnapshot
    def usage(self) -> UsageSnapshot
    def models(self) -> list[ProviderModels]
    def sessions(self, limit: int = 10) -> list[SessionRecord]

COMMAND_NAMES = ("code", "status", "usage", "models", "sessions", "cancel")
REF_RE = re.compile(r"^[0-9a-f-]{6,36}$")

async def run_command(name: str, *, cfg: DiscordConfig, backend: BotBackend, ctx: CommandContext,
                      redactor: SecretRedactor | None, options: Mapping[str, str | None]) -> Reply
```
`run_command` steps:
1. Call `authorize`. If it is denied, log at WARNING `discord auth denied command=<name> user=<id> reason=<reason>` (never the options) and return `Reply("Not authorized.", ephemeral=True)`.
2. Dispatch. An unknown name gives `Reply("Unknown command.", True)`.
3. `/code` is non-ephemeral. Everything else is ephemeral.

Rules for each command:
- **`/code`:** `prompt` is required. `project` defaults to `"default"`. `session` is optional; it is lower-cased first and must match `REF_RE` (otherwise `UNKNOWN_SESSION`). Each `SubmitStatus` maps to a one-line reply.
- **`/cancel`:** `task` is lower-cased first and must match `REF_RE`, otherwise `Invalid task id.`

Every exception from the backend is caught. The reply is `Internal error (<ExceptionClass>).`, and it is logged with the class only.

### 8.4 `app/bot/backend.py`
`ControllerBackend(controller: AgentController, config: AppConfig, *, environ=None)` implements `BotBackend`:
- **`submit_code`:**
  - It sanitizes the prompt with `sanitize_prompt` and checks the project with `project_directory`.
  - It resolves `session_ref` with `SessionRepository.find_by_ref`; the session must be OPEN.
  - Then it calls `controller.enqueue_task`, with the ctx ids converted to str (None stays None).
  - `queue_position` is `controller.metrics()["task_queue_depth"]`.
- **`cancel`:** `TaskRepository.find_by_ref`, then `controller.cancel_task`.
- **`status`:** `await orchestrator.executor.health_check()`, plus `orchestrator.running`, `metrics()["task_queue_depth"]` and `TaskRepository.list_recent(5)`.
- **`usage`:**
  - `exact = QuotaSnapshotRepository(conn).latest(now)`, without estimate-prefixed sources (as in `app/quota/state.py::_build`);
  - plus `estimate_quota(config, UsageRepository(conn), now, exact=exact)`;
  - plus `orchestrator.cooldowns.active(now)`;
  - plus `UsageRepository.daily_totals(now)`.
- **`models`:** for each policy id in `provider_order`, call `model_listing(config, provider, ProviderModelRepository(conn).list_model_ids(provider))`, keeping only the listings of that policy plus the DISCOVERED_ONLY ones. The capabilities come from `allowed_capabilities`.
- **`sessions`:** `SessionRepository.list_recent(limit)`.

### 8.5 `app/bot/discord_frontend.py`
```python
def create_frontend(config: AppConfig, *, redactor: SecretRedactor | None,
                    environ: Mapping[str, str] | None = None) -> ChatFrontend
```
It returns `NullFrontend` and logs the WARNING of Decision 13 when `resolve_secret(config.discord.bot_token_env, environ)` is None. Otherwise it returns `DiscordFrontend(config, redactor=..., environ=...)`. The token is resolved again inside `start`; never store it on `self`.

`DiscordFrontend`:
- **`__init__`** creates nothing network-related.
- **`build_client(self) -> tuple[discord.Client, app_commands.CommandTree]`** is public so tests can inspect the commands without connecting.
  - `intents = discord.Intents.none(); intents.guilds = True`.
  - Register the six commands with descriptions:
    - `/code prompt:str(max_length=4000) project:str? session:str?`
    - `/status`
    - `/usage`
    - `/models`
    - `/sessions`
    - `/cancel task:str`
  - `setup_hook` copies global commands to each allowed guild and syncs per guild (`tree.copy_global_to(guild=discord.Object(id))`, `await tree.sync(guild=...)`). A sync failure logs the class name only.
- **`start(controller)`:**
  - Build `ControllerBackend` and the client.
  - Then `self._task = asyncio.create_task(client.start(token))` (**not** `client.run`, which installs its own logging handler).
  - Add a done-callback that logs `discord frontend stopped: <ExcClass>` on error (for example `LoginFailure`). The daemon keeps running.
- **Each command callback:**
  1. Build `CommandContext` from `interaction.user.id`, `interaction.guild_id` and `interaction.channel_id`.
  2. Call `authorize` first. If denied, `interaction.response.send_message("Not authorized.", ephemeral=True)`, log as in §8.3, and return.
  3. Otherwise `await interaction.response.defer(ephemeral=<command is not "code">, thinking=True)` immediately.
  4. Then `reply = await run_command(...)`.
  5. Then `await interaction.followup.send(reply.content, ephemeral=reply.ephemeral, allowed_mentions=AllowedMentions.none())`.

  `run_command` re-authorizes, which is harmless.
- **`on_event(event)`:**
  1. `text = format_event(event, redactor=...)`. If it is None, or `channel_id` is missing, return.
  2. Resolve the channel with `client.get_channel(int(channel_id))`. If that is None, skip and log at DEBUG.
  3. For `progress`: edit the task's existing progress message if one is tracked, otherwise send one and track it.
  4. Any other event: send a new message.
  5. Track messages in an `OrderedDict[str, discord.Message]` capped at 100 (LRU eviction). On `task_finished`, drop the entry.
  6. Every send/edit uses `AllowedMentions.none()`.
  7. Exceptions are caught and logged with the class name.

  Provide a test seam: a `_client` attribute that tests can set to a stub exposing `get_channel`.
- **`stop()`:** `await client.close()`, then await or cancel `_task` with a 10 s timeout. It is idempotent, and safe when `start` was never called.

## 9. Test fakes

### 9.1 `tests/fakes/fake_opencode.py` (stdlib only)
- Run it as `[sys.executable, <abs path>]`, which makes up `opencode.command`. `shutil.which` resolves `sys.executable`, an absolute path.
- On each invocation it appends `json.dumps(sys.argv[1:])` plus a newline to `$FAKE_OC_ARGV_FILE` (if that is set) and records `os.getcwd()` the same way in `$FAKE_OC_CWD_FILE`.
- `$FAKE_OC_SCENARIOS` is a comma list. The n-th invocation (the counter is a file at `$FAKE_OC_COUNTER_FILE`) uses item n, and the last item repeats.
- Tests pass these variables through `OpenCodeExecutor(extra_env=...)`.

The scenarios are:

| Scenario | Behavior |
|---|---|
| `success` | `step_start`(sessionID `ses_fake1`), `text`, `tool_use` edit (`filePath` `src/a.py`), `step_finish` (tokens 10/5), exit 0 |
| `rate_limit` | session event, then `error` APIError statusCode 429 with `responseHeaders {"retry-after":"30"}`, exit 1 |
| `quota_text` | session event, then `error` UnknownError with message `Resource has been exhausted (e.g. check quota).`, exit 1 |
| `auth` | `error` ProviderAuthError, exit 1 |
| `no_session_429` | an `error` with statusCode 429 and no sessionID anywhere, exit 1 |
| `tool_no_session` | a `tool_use` event without sessionID, then exit 1 with stderr `boom` |
| `exit_nonzero` | stderr `boom`, exit 3 |
| `error_exit0` | session, then a 503 error event, exit 0 |
| `garbage` | non-JSON lines and `[]`, then `success` |
| `hang` | session event, flush, sleep 120 |
| `spawn_child` | spawns a grandchild (`sys.executable -c` loop appending a line to `$FAKE_OC_HEARTBEAT` every 0.1 s), emits a session event, sleeps 120 |
| `flood` | 20 000 `text` events of 1 000 chars each, then exit 0 |
| `long_line` | one 3 MB non-JSON line, then a newline, then a `success` sequence |
| `secret_echo` | `text` containing `$FAKE_OC_SECRET`, then exit 0 |

The fake must flush stdout after every line.

### 9.2 `tests/fakes/fake_executor.py`
`ScriptedExecutor(results: list[ExecutionResult | Callable[..., Awaitable[ExecutionResult]]], health: ExecutorHealth = healthy)`:
- It records every `run` kwarg dict in `.calls`.
- It returns the scripted items in order; the last item repeats.
- A callable item is awaited with the run kwargs. This supports cancel tests, where the item awaits `cancel_event.wait()` and returns CANCELLED, and can fire `on_event`.
- `aclose` is a no-op.

## 10. Tests (every test is offline; use the `db`/`make_config` fixtures and `NOW`/`secret_env` from `tests/conftest.py`)

**`test_opencode_events.py`**
- Each event type parses.
- The session id comes from the top level and from `part`. An invalid session id (`-x`, too long, spaces) is ignored.
- Non-JSON input, a non-dict, and a missing `type` each give None.
- A bool token is rejected.
- Classification:
  - ProviderAuthError → AUTH_FAILED;
  - 429 → RATE_LIMITED;
  - 429 with quota text → QUOTA_EXHAUSTED;
  - 504 with `rate limit` text → stays TIMEOUT_UNKNOWN_OUTCOME;
  - 400 with context text → CONTEXT_TOO_LARGE;
  - no status with `Resource has been exhausted` → QUOTA_EXHAUSTED;
  - MessageOutputLengthError → CONTEXT_TOO_LARGE;
  - garbage → UNKNOWN.
- Retry-after is parsed case-insensitively.
- Nothing raises on arbitrary nested junk.

**`test_opencode_executor.py`** (real subprocesses of the fake; wrap each test in a 60 s `asyncio.wait_for`)
- `build_argv`:
  - order and flags;
  - `--session` only when given;
  - the provider_map maps gemini to `google/<m>`;
  - openrouter gives `openrouter/openrouter/free`;
  - a leading-`-` prompt gets a space prefix.
- `success`: SUCCEEDED, `session_id="ses_fake1"`, `changed_files == ("src/a.py",)`, tokens 10/5, `llm_steps == 1`, the cwd equals `project_dir`, and the argv file shows `--format json`.
- `rate_limit`: FAILED, RATE_LIMITED, http 429, `retry_after_s == 30`.
- `quota_text`: QUOTA_EXHAUSTED.
- `auth`: AUTH_FAILED.
- `exit_nonzero`: FAILED, UNKNOWN, exit 3.
- `error_exit0`: FAILED, PROVIDER_UNAVAILABLE.
- `garbage`: SUCCEEDED.
- `hang` with `run_timeout_s=1`: TIMED_OUT, TIMEOUT_UNKNOWN_OUTCOME, and returns well under 30 s.
- `hang` with the cancel event set after the session event: CANCELLED.
- `spawn_child` cancel: after the result plus 1 s, the heartbeat file size stays unchanged over a further 1 s (the process tree is dead). This must pass on Windows (taskkill) and on POSIX.
- `flood`: `len(output_tail) <= output_tail_chars` and `output_truncated`.
- `long_line` with `max_line_bytes=1_048_576`: SUCCEEDED (the oversized line is skipped).
- `secret_echo` with a redactor holding the secret: the secret is absent from `output_tail`.
- Missing executable: EXECUTOR_UNAVAILABLE.
- Missing project dir: EXECUTOR_UNAVAILABLE.
- On Windows only (`skipif` otherwise): a `.cmd` file in tmp gives EXECUTOR_UNAVAILABLE.
- Empty prompt or a 4001-char prompt: FAILED/INVALID_REQUEST with no spawn (the argv file is absent).
- Outer task cancelled (`CancelledError`) on `spawn_child`: the tree is killed (heartbeat stops) and CancelledError propagates.

**`test_opencode_health.py`** (MockTransport)
- Healthy, with the version.
- `healthy: false`.
- 500.
- Non-JSON.
- ConnectError raised by the handler.
- The URL path is exactly `/global/health` when `server_url` has a trailing slash.

**`test_sessions_attempts_repo.py`**
- create/get, and `find_by_ref` with an exact match, a unique prefix and an ambiguous prefix (raises).
- `set_opencode_session_id` sets the id only once.
- `close_idle` respects the cutoff and unfinished tasks.
- Attempts are started and finished in order, and `retry_after_ms` is clamped.
- `TaskRepository.set_selection` sets cost_class FREE.
- `TaskRepository.attach_session`, `find_by_ref` and `list_recent` order.

**`test_orchestrator.py`** (ScriptedExecutor + `FakeProviderAdapter`s from `app/providers/fake.py` returning tool-capable, 200k-context capabilities for every allowlisted model, as in `full_state`; `environ=secret_env()`; drive it through a started `AgentController(cfg, orchestrator=orch)` with `enqueue_task`, then `await asyncio.wait_for(controller._task_queue.join(), 10)`. Set the database path under `tmp_path` and `opencode.working_directory` to `str(tmp_path)`.)
- **Success:** first candidate `openrouter/openrouter/free`; the task is SUCCEEDED, cost_class FREE, `selected_*` set; 1 attempt row SUCCEEDED; a usage row FREE.
- **T054 chain:**
  - OR 429 (with session `ses_fake1`) → gemini g-3.8 429 → g-3.7 success. The attempts are persisted in order with error_class and http_status.
  - The cooldowns contain `(openrouter, openrouter/free)` and `(gemini, g-3.8)`.
  - The second and third calls have `session_id == "ses_fake1"` and `prompt == continue_prompt`.
  - The task session's `opencode_session_id` is set.
  - Run this with `max_fallback_attempts: 5`.
- **No session captured** (results without `session_id`, 429): the second call re-sends the original prompt with `session_id=None`.
- **Tool activity without a session:** FAILED with `UNKNOWN` after 1 attempt.
- **Every candidate rate-limited:** two configs.
  - `max_fallback_attempts: 10` → FREE_CAPACITY_EXHAUSTED after 5 attempts.
  - `max_fallback_attempts: 2` → MAX_FALLBACK_ATTEMPTS_REACHED after 2 attempts.
  - In both, the executor is never called after the stop.
- **TIMED_OUT:** FAILED with TIMEOUT_UNKNOWN_OUTCOME, 1 attempt, no replay.
- **AUTH_FAILED on OR:** the next candidate is gemini (provider excluded).
- **Health unhealthy:** FAILED with `OPENCODE_UNAVAILABLE`, 0 attempts, the executor's `run` not called, no cooldowns.
- **EXECUTOR_UNAVAILABLE result:** FAILED with `OPENCODE_UNAVAILABLE`, no cooldown.
- **Cancel while running:** `request_cancel` → CANCELLING → the task ends CANCELLED.
- **Cancel while queued** (block the worker on the first task with a gate): the second task becomes CANCELLED and its executor is never called.
- **`recover_interrupted`:** pre-seeded RUNNING → FAILED `INTERRUPTED_BY_RESTART`; QUEUED → CANCELLED.
- **Shutdown during a run:** `controller.stop()` → the task ends CANCELLED with `DAEMON_SHUTDOWN`.
- **Follow-up:** a submission with an existing session (that has an OpenCode id) sends `session_id=<oc id>` with the new prompt.
- **Unknown project:** FAILED with INVALID_REQUEST and 0 attempts.
- **Events:** with `progress_min_interval_s=10` and a fixed clock, a callable script item that fires 1 SESSION and 50 rapid TOOL events produces at most 2 `progress` events (SESSION + 1).
- **No PAID anywhere:** after the chain test, `SELECT COUNT(*) FROM usage_events WHERE cost_class <> 'FREE'` is 0.
- **Integration:** one test with the real `OpenCodeExecutor` (fake script, scenarios `rate_limit,success`, MockTransport health returning healthy) reaches SUCCEEDED on the second candidate, with `--session ses_fake1` in the second argv.

**`test_bot_auth.py`**
- Every list is empty → denied.
- User allowed, but guild or channel lists empty → denied.
- DM → `dm_not_allowed`.
- Wrong user, guild or channel → the right reason.
- All three match → allowed.

**`test_bot_format.py`**
- `safe_text` truncates to ≤ 1900 chars, neutralizes ```` ``` ````, strips ANSI and redacts the secret.
- `format_event` for each event name, including the explicit FREE_CAPACITY_EXHAUSTED line and the unknown-outcome continue hint. `task_routing` gives None.
- `format_usage`: EXACT shows the number, ESTIMATED shows `~`, UNKNOWN shows no digits for that row, and the no-data message.
- `format_models`: ALLOWED ranks and the DISCOVERED_ONLY count.
- `format_status`: healthy and unreachable variants.

**`test_bot_commands.py`** (fake BotBackend)
- An unauthorized user gets "Not authorized." and the backend is not called. A log record is captured and does not contain the prompt text.
- `/code` is QUEUED → the ack contains the short id and is non-ephemeral.
- Each SubmitStatus gets its reply.
- `/cancel` with an invalid ref → "Invalid task id."; with a valid ref it passes through.
- `/status`, `/usage`, `/models` and `/sessions` are ephemeral.
- A backend exception gives `Internal error (RuntimeError).`
- A secret inside backend data is redacted in the reply.

**`test_discord_frontend.py`**
- `create_frontend` with an empty environ returns a `NullFrontend` and logs the env var **name**.
- `create_frontend` with a token returns a `DiscordFrontend`, and the token value does not appear in `repr(frontend)` or `vars(frontend)`.
- `build_client()` (no start) registers exactly `COMMAND_NAMES`, and `/code` has a `prompt` option with `max_length == 4000`.
- `on_event` with a stub client (`get_channel` returns a fake channel recording `send`/`edit`) does the following:
  - progress edits a single message;
  - `task_finished` sends a new message;
  - a missing `channel_id` sends nothing;
  - `allowed_mentions` is `AllowedMentions.none()`.
- `stop()` without `start()` does not raise.

**`test_wiring.py`**
- `build_runtime(config, redactor=..., environ={}, once=False)` gives a NullFrontend, an `OpenCodeExecutor` and an `Orchestrator` bound to the controller after start. `close_runtime` calls `aclose` on the adapters.
- `once=True` always gives a NullFrontend, even with a token in environ.

**Config tests** (add to `tests/test_config.py`)
- The new defaults.
- `provider_map` values containing `/` are rejected.
- The `projects` key `default` is rejected.
- An http base_url for a non-localhost host is rejected, while `http://localhost:8080` is accepted.
- `resolve_secret` strips.

**Daemon tests** (add to `tests/test_daemon.py`)
- `enqueue_task` returns the record, and returns None when the queue is full or the controller is stopped.
- A frontend whose `on_event` raises does not stop the event loop (a later event is still delivered).

## 11. CLI wiring (`app/runtime/wiring.py`, `app/cli.py`)
```python
@dataclass(slots=True)
class Runtime:
    controller: AgentController
    orchestrator: Orchestrator
    executor: OpenCodeExecutor
    adapters: dict[str, HttpProviderAdapter]

def build_runtime(config: AppConfig, *, redactor: SecretRedactor, environ: Mapping[str, str] | None = None,
                  once: bool = False) -> Runtime
async def close_runtime(rt: Runtime) -> None     # aclose adapters + executor; each guarded, logs class name only
```
`build_runtime` wires:
- `adapters = build_adapters(config, environ=environ, redactor=redactor)`;
- `executor = OpenCodeExecutor(config.opencode, redactor=redactor)`;
- `orchestrator = Orchestrator(config, executor=executor, adapters=adapters, environ=environ)`;
- `frontend = NullFrontend() if once else create_frontend(config, redactor=redactor, environ=environ)`;
- `controller = AgentController(config, frontend=frontend, orchestrator=orchestrator)`.

Import `app.bot.discord_frontend` lazily inside `build_runtime` (only when `once` is False), so that `--check` and `--once` never import discord.

In `cli.py`:
- Replace the non-`--check` path with `asyncio.run(_serve(config, redactor, once=args.once))`.
- `_serve` builds the runtime **inside** the running loop, installs the signal handlers as today, and runs `run_once()` or `run_forever()`.
- `close_runtime` runs in a `finally`.
- `--check` is unchanged.
- `tests/test_cli.py` must pass unchanged.

## 12. Doc fix
In `docs/SETUP-discord-opencode.md` Part E, delete exactly the line `git checkout feat/providers-quota      # or main once branches are merged`. Change nothing else in the doc.

## 13. Deferred (do not implement; carry forward in changes.md)
- **Approvals.** T060 and T061 (approval buttons, action interception) and `/approve`/`/deny` are not trivial, because OpenCode permission events would need the server API. `/attach` is deferred too.
- **Start scripts.** T101/T102 (start scripts, auto-start) and T082 (live end-to-end).
- **OpenCode server auth.** Basic auth for the OpenCode server (`OPENCODE_SERVER_PASSWORD`) in the health check and in `--attach`.
- **OpenCode internal retry visibility (B4).** Detecting 429 retries before the timeout fires.
- **Slice-2 review follow-ups:**
  - snapshot staleness max-age (before T044);
  - code map vs. 504 precedence in `http.py`;
  - ratelimit header overflow clamps;
  - partial-key and other-scheme redaction;
  - `install_redaction` covering handlers added later. No new handlers are added in this slice, and discord.py `client.start` adds none;
  - A9 per-model Groq header scoping;
  - probe-cost reuse;
  - test_cli redaction-filter hygiene;
  - all slice-1 test-strengthening items.
- **Strict xfails.** The two strict xfails stay as they are. The paid-spend one is moot now that paid usage cannot be recorded. The reviewer may recommend deleting it.
- **Quota refresh scheduler (T044).** Refresh happens only per task, rate-limited by `provider_refresh_interval_s`.
- **More than one concurrent task.**
