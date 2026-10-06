"""Key-holding LLM gateway: the only place real provider keys are used.

OpenCode is configured with this gateway as its single provider (see
``app/opencode/server.py``). It authenticates with a random per-run token, never with a real
provider key. The gateway forwards ONLY allowlisted free models (``AppConfig.gateway_models``),
rewrites ``model`` to the upstream id, drops every request key that could select another or a
paid model (OpenRouter ``models``/``route``/``provider``/``plugins``/``transforms``), and sends
the request to the adapter's chat URL and nowhere else.

Assumptions (verify at T082; see SPEC section 16):

G1. ``@ai-sdk/openai-compatible`` sends ``Authorization: Bearer <options.apiKey>`` to
    ``POST {baseURL}/chat/completions``. Streaming is SSE (``data: {json}`` lines and
    ``data: [DONE]``); the final chunk may carry ``usage`` (prompt_tokens/completion_tokens).
G2. Gemini's OpenAI-compatible endpoint is ``{base_url}/openai/chat/completions`` with Bearer
    auth. Its error bodies may not match the native shape; classification then falls back to
    the HTTP status.
G3. OpenRouter and Groq: ``{base_url}/chat/completions`` with Bearer auth.

Upstream bodies are read through httpx's decoding iterator and ``Accept-Encoding: identity`` is
sent, so a relayed body never carries a stale ``Content-Encoding``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import datetime

import httpx
from aiohttp import web

from app.config.models import AppConfig
from app.db.usage import UsageEvent, UsageStatus
from app.providers.base import CostClass, ErrorClass
from app.providers.http import HttpProviderAdapter, classify_transport_error
from app.quota.cooldown import CooldownManager
from app.redaction import SecretRedactor
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

ALLOWED_REQUEST_KEYS = frozenset(
    {
        "messages",
        "stream",
        "stream_options",
        "temperature",
        "top_p",
        "max_tokens",
        "max_completion_tokens",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "seed",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "user",
        "reasoning_effort",
        "n",
    }
)
_COOLDOWN_CLASSES = frozenset(
    {ErrorClass.RATE_LIMITED, ErrorClass.QUOTA_EXHAUSTED, ErrorClass.PROVIDER_UNAVAILABLE}
)
_LINE_BUFFER_CAP = 1_048_576
_MAX_LOGGED_DROPPED = 10
_Tokens = tuple[int | None, int | None]


def _json_error(
    status: int,
    message: str,
    type_: str,
    code: str | None,
    headers: Mapping[str, str] | None = None,
) -> web.Response:
    body = {"error": {"message": message, "type": type_, "code": code}}
    return web.json_response(body, status=status, headers=headers)


def _unsupported_value(body: Mapping[str, object]) -> tuple[str, str] | None:
    """Only plain text and function tools may reach a provider.

    Provider-hosted tools (e.g. Groq ``browser_search``/``code_interpreter``) and non-text content
    parts (``file``, ``image_url``, ``input_audio``; OpenRouter file handling can use paid OCR) are
    billed outside the free plan. Returns ``(message, code)``; the message never echoes content.
    """
    tools = body.get("tools")
    if tools is not None:
        if not isinstance(tools, list) or any(
            not isinstance(t, dict) or t.get("type") != "function" for t in tools
        ):
            return "only function tools are supported", "unsupported_tool_type"
    choice = body.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") != "function":
        return "only function tool_choice is supported", "unsupported_tool_type"
    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list) and any(
                not isinstance(p, dict) or p.get("type") != "text" for p in content
            ):
                return "only text content parts are supported", "unsupported_content_type"
    return None


def _valid_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _usage_from_obj(obj: object) -> _Tokens | None:
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    tin = _valid_count(usage.get("prompt_tokens"))
    tout = _valid_count(usage.get("completion_tokens"))
    if tin is None or tout is None:
        return None
    return tin, tout


class _SseUsage:
    """Incremental SSE line parser that keeps only the last ``usage`` seen."""

    def __init__(self) -> None:
        self._buf = b""
        self._disabled = False
        self.tokens: _Tokens = (None, None)

    def feed(self, chunk: bytes) -> None:
        if self._disabled:
            return
        self._buf += chunk
        *lines, rest = self._buf.split(b"\n")
        self._buf = rest
        if len(self._buf) > _LINE_BUFFER_CAP:
            self._disabled = True
            self._buf = b""
        for line in lines:
            self._line(line.strip())

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            obj = json.loads(data)
        except ValueError:
            return
        found = _usage_from_obj(obj)
        if found is not None:
            self.tokens = found


class LlmGateway:
    def __init__(
        self,
        config: AppConfig,
        *,
        adapters: Mapping[str, HttpProviderAdapter],
        cooldowns: CooldownManager,
        usage_sink: Callable[[UsageEvent], None],
        current_task_id: Callable[[], str | None],
        redactor: SecretRedactor | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        token: str | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._config = config
        self._gw = config.gateway
        self._adapters = adapters
        self._cooldowns = cooldowns
        self._usage_sink = usage_sink
        self._current_task_id = current_task_id
        self._redactor = redactor
        self._clock = clock
        self.token: str = token if token is not None else secrets.token_urlsafe(32)
        self.models: dict[str, tuple[str, str]] = config.gateway_models()
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(
                connect=self._gw.upstream_connect_timeout_s,
                read=self._gw.upstream_read_timeout_s,
                write=30,
                pool=self._gw.upstream_connect_timeout_s,
            ),
        )
        self._sem = asyncio.Semaphore(self._gw.max_concurrent_requests)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    # ---- lifecycle -------------------------------------------------

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("gateway not started")
        return f"http://127.0.0.1:{self._port}/v1"

    def build_app(self) -> web.Application:
        app = web.Application(client_max_size=self._gw.max_request_bytes)
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._list_models)
        app.router.add_route("*", "/{tail:.*}", self._not_found)
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", self._gw.port)
            await site.start()
            self._port = int(runner.addresses[0][1])
        except BaseException:
            await runner.cleanup()
            raise
        self._runner = runner
        logger.info(
            "llm gateway listening on 127.0.0.1:%d (%d models)", self._port, len(self.models)
        )

    def is_running(self) -> bool:
        return self._runner is not None

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            await runner.cleanup()
        await self._client.aclose()

    # ---- helpers ---------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        got = request.headers.get("Authorization", "")
        want = f"Bearer {self.token}"
        return hmac.compare_digest(
            got.encode("utf-8", errors="replace"), want.encode("utf-8", errors="replace")
        )

    @staticmethod
    def _unauthorized() -> web.Response:
        return _json_error(401, "invalid api key", "invalid_request_error", "invalid_api_key")

    def _scrub(self, text: str) -> str:
        if self._redactor is not None:
            text = self._redactor.redact(text)
        return text.replace(self.token, "[REDACTED]") if self.token else text

    def _record(
        self,
        provider: str,
        model: str,
        status: UsageStatus,
        tokens: _Tokens = (None, None),
        request_id: str | None = None,
    ) -> None:
        try:
            self._usage_sink(
                UsageEvent(
                    provider=provider,
                    model=model,
                    cost_class=CostClass.FREE,
                    status=status,
                    occurred_at=self._clock(),
                    task_id=self._current_task_id(),
                    request_id=request_id[:100] if request_id else None,
                    input_tokens=tokens[0],
                    output_tokens=tokens[1],
                )
            )
        except Exception as e:
            logger.warning("gateway usage sink failed: %s", type(e).__name__)

    def _cooldown_retry_after(self, provider: str, model: str) -> int:
        active = self._cooldowns.active()
        now = self._clock()
        left = [
            (until - now).total_seconds()
            for key in ((provider, None), (provider, model))
            if (until := active.get(key)) is not None
        ]
        return max(1, math.ceil(max(left, default=1.0)))

    # ---- routes ----------------------------------------------------

    async def _not_found(self, request: web.Request) -> web.Response:
        return _json_error(404, "not found", "invalid_request_error", "not_found")

    async def _list_models(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return self._unauthorized()
        data = [{"id": gid, "object": "model", "owned_by": "broke"} for gid in self.models]
        return web.json_response({"object": "list", "data": data})

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        started = time.monotonic()
        ctx: dict[str, str] = {}
        resp: web.StreamResponse | None = None
        try:
            resp = await self._chat_inner(request, ctx)
            return resp
        finally:
            logger.info(
                "gateway %s %s model=%s status=%s duration_ms=%d",
                request.method,
                request.path,
                ctx.get("gid", "-"),
                resp.status if resp is not None else "aborted",
                (time.monotonic() - started) * 1000,
            )

    async def _chat_inner(self, request: web.Request, ctx: dict[str, str]) -> web.StreamResponse:
        if not self._authorized(request):
            return self._unauthorized()
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return _json_error(
                413, "request body too large", "invalid_request_error", "request_too_large"
            )
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _json_error(
                400, "body must be a JSON object", "invalid_request_error", "invalid_json"
            )
        gid = body.get("model")
        if not isinstance(gid, str) or gid not in self.models:
            return _json_error(
                403, "model is not allowlisted", "invalid_request_error", "model_not_allowlisted"
            )
        ctx["gid"] = gid
        provider, model = self.models[gid]

        if self._cooldowns.is_cooling(provider, model):
            return _json_error(
                429,
                "model is cooling down",
                "rate_limit_error",
                "rate_limited",
                {"Retry-After": str(self._cooldown_retry_after(provider, model))},
            )

        adapter = self._adapters.get(provider)
        auth = adapter.chat_headers() if adapter is not None else None
        if adapter is None or auth is None:
            return _json_error(
                503,
                "upstream credential is not configured",
                "server_error",
                "upstream_credential_missing",
            )

        rejection = _unsupported_value(body)
        if rejection is not None:
            return _json_error(400, rejection[0], "invalid_request_error", rejection[1])

        dropped = [k for k in body if k not in ALLOWED_REQUEST_KEYS and k != "model"]
        if dropped:
            logger.warning(
                "gateway dropped request keys: %s",
                [str(k)[:40] for k in dropped[:_MAX_LOGGED_DROPPED]],
            )
        payload = {k: v for k, v in body.items() if k in ALLOWED_REQUEST_KEYS}
        payload["model"] = model
        streaming = body.get("stream") is True
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if streaming else "application/json",
            "Accept-Encoding": "identity",
            **auth,
        }

        async with self._sem:
            try:
                async with self._client.stream(
                    "POST",
                    adapter.chat_url(),
                    headers=headers,
                    content=json.dumps(payload).encode(),
                ) as up:
                    return await self._relay(request, adapter, provider, model, up, streaming)
            except Exception as e:
                return self._transport_failure(provider, model, e)

    def _transport_failure(self, provider: str, model: str, e: Exception) -> web.Response:
        cls = classify_transport_error(e)
        name = type(e).__name__  # class name only: exception text could echo request details
        if cls == ErrorClass.TRANSIENT_NETWORK:
            return _json_error(
                502, f"upstream unreachable ({name})", "upstream_error", "upstream_unreachable"
            )
        self._record(provider, model, UsageStatus.UNKNOWN_OUTCOME)
        return _json_error(504, f"upstream timeout ({name})", "upstream_error", "upstream_timeout")

    async def _relay(
        self,
        request: web.Request,
        adapter: HttpProviderAdapter,
        provider: str,
        model: str,
        up: httpx.Response,
        streaming: bool,
    ) -> web.StreamResponse:
        request_id = up.headers.get("x-request-id") or up.headers.get("request-id")
        if not up.is_success:
            return await self._relay_error(adapter, provider, model, up, request_id)
        self._cooldowns.record_success(provider)
        ctype = up.headers.get("content-type", "")
        if streaming and ctype.lower().startswith("text/event-stream"):
            return await self._relay_stream(request, provider, model, up, request_id)
        return await self._relay_body(provider, model, up, request_id)

    async def _relay_error(
        self,
        adapter: HttpProviderAdapter,
        provider: str,
        model: str,
        up: httpx.Response,
        request_id: str | None,
    ) -> web.Response:
        cap = self._gw.max_error_body_bytes
        buf = bytearray()
        async for chunk in up.aiter_bytes():
            buf += chunk
            if len(buf) >= cap:
                break
        rebuilt = httpx.Response(up.status_code, headers=up.headers, content=bytes(buf[:cap]))
        err = adapter.error_from_response(rebuilt)
        message = self._scrub(err.message)
        code = self._scrub(err.error_code) if err.error_code else None
        headers: dict[str, str] = {}
        if err.retry_after_s is not None and math.isfinite(err.retry_after_s):
            headers["Retry-After"] = str(max(0, math.ceil(err.retry_after_s)))
        self._record(provider, model, UsageStatus.ERROR, request_id=request_id)
        if err.error_class in _COOLDOWN_CLASSES:
            self._cooldowns.record_failure(
                provider, model, err.error_class, retry_after_s=err.retry_after_s
            )
        status = 502 if 300 <= up.status_code < 400 else up.status_code
        return _json_error(status, message, "upstream_error", code, headers)

    async def _relay_body(
        self, provider: str, model: str, up: httpx.Response, request_id: str | None
    ) -> web.Response:
        cap = self._gw.max_response_bytes
        buf = bytearray()
        async for chunk in up.aiter_bytes():
            buf += chunk
            if len(buf) > cap:
                self._record(provider, model, UsageStatus.UNKNOWN_OUTCOME, request_id=request_id)
                return _json_error(
                    502,
                    "upstream response too large",
                    "upstream_error",
                    "upstream_response_too_large",
                )
        data = bytes(buf)
        tokens: _Tokens = (None, None)
        try:
            found = _usage_from_obj(json.loads(data))
        except ValueError:
            found = None
        if found is not None:
            tokens = found
        self._record(provider, model, UsageStatus.SUCCESS, tokens, request_id)
        return web.Response(status=up.status_code, body=data, content_type="application/json")

    async def _relay_stream(
        self,
        request: web.Request,
        provider: str,
        model: str,
        up: httpx.Response,
        request_id: str | None,
    ) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        usage = _SseUsage()
        status = UsageStatus.UNKNOWN_OUTCOME
        broken = False
        try:
            await resp.prepare(request)
            async for chunk in up.aiter_bytes():
                usage.feed(chunk)
                await resp.write(chunk)
            await resp.write_eof()
            status = UsageStatus.SUCCESS
        except ConnectionResetError:
            broken = True  # client went away; leaving the stream context closes upstream
        except Exception as e:
            broken = True
            logger.warning("gateway upstream stream failed: %s", type(e).__name__)
        finally:
            self._record(provider, model, status, usage.tokens, request_id)
        if broken and request.transport is not None:
            request.transport.close()
        return resp
