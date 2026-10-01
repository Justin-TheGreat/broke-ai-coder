# Test results: slice 2 (providers, cooldowns, quota, redaction)

Status: ALL PASS. 635 passed, 2 xfailed (strict, documented findings). `ruff check .` clean, `ruff format --check .` clean (83 files). No application code was changed.

## Added
`tests/test_slice2_audit.py` (about 270 cases, offline, httpx.MockTransport and dummy keys only). The coder's 366 existing tests were audited against spec section 10.2; the gaps below were filled.

- classify_error for all 4 providers across statuses 400/401/404/408/413/422/429/5xx/504, each with JSON, HTML, empty and list bodies (class always follows status, never raises, also via `httpx.HTTPStatusError`).
- 403 gives AUTH_FAILED (Gemini, Cerebras, Groq); OpenRouter 402 gives QUOTA_EXHAUSTED and 403 gives POLICY_REJECTED; 402 elsewhere gives INVALID_REQUEST.
- Retry-After as seconds, HTTP-date, "0" (0.0, not None), decimals, garbage (None). Gemini RetryInfo body variants (1.5s, garbage, non-string). Header wins over body.
- Gemini: API_KEY_INVALID on 400 buried among other details items gives AUTH_FAILED and health DOWN; plain 400 stays INVALID_REQUEST; gRPC status names map correctly.
- Groq/Cerebras `code` map including case-insensitive and unmapped-code fallback.
- Transport failures: connect-phase gives TRANSIENT_NETWORK (safe to replay); read/write/remote-protocol/TimeoutError gives TIMEOUT_UNKNOWN_OUTCOME; malformed 2xx JSON gives MalformedResponseError/UNKNOWN and health DEGRADED; non-HTTP exceptions never escape `health_check`.
- Quota labelling: Groq exact values (limit/remaining/used/reset_at, ms and minute-second durations); remaining "0" is EXACT 0 not None; unparseable, empty, negative, NaN and reset-only headers give `[]`; Groq and Cerebras header names are not cross-parsed; Cerebras seconds reset; Gemini never EXACT; OpenRouter string limit not EXACT; estimator output never EXACT, and UNKNOWN with `remaining=None` when tokens are missing.
- Cooldown: 429 cools only that model and routes g-3.8 to g-3.7; g-3.8 and g-3.7 both cooled go to the next provider (cerebras) when OpenRouter is exhausted; staggered expiry returns to g-3.8; provider-wide cooldown skips all Gemini models; 3 network failures cool the provider then expire; Retry-After clamp boundaries (equal to max, just above, negative, -inf, 1e12); `active()` returns a copy.
- RouterState: unlisted models never enter, whether from observations, the DB fallback, or when stored status was tampered with; negative, -inf, inf and NULL paid costs (today only, or month only) raise RouterStateError; `route_with_state` returns ROUTER_STATE_UNAVAILABLE and never Selected even though free models are eligible; a closed DB connection is RouterStateError, not sqlite3.Error; real zero spend stays 0.0.
- Redaction: Authorization/x-api-key/x-goog-api-key headers, `?key=` and `&api_key=` URLs, unconfigured secrets via header patterns, `httpx.Request` repr, chained exception tracebacks (cause chain, `exc_info=True`, explicit exc_info), child loggers, dict args, stack_info, secret added after install; reprs of redactor, adapters, observations and errors; adapters hold no copy of the resolved key; full adapter failure flows (4 adapters, 3 echo shapes) never leak into errors, tracebacks, health detail, or adapter log records; `refresh_all` against echoing 401s leaves no key in caplog, observations repr or DB tables; Gemini key never in any paginated or error URL.

## Regression checks (mutate, confirm failure, restore byte-exact via `cmp` against backups)
1. `app/quota/state.py`: clamp spend with `max(total, 0.0)` gave 7 failures (new fail-closed tests and the coder's `test_bad_spend_negative` and `test_route_with_state_fails_closed`). Restored.
2. `app/redaction.py`: remove redaction of `exc_info` text gave 3 failures (new traceback test and 2 of the coder's). Restored.
3. `app/quota/cooldown.py`: make 429 key `(provider, None)` gave 9 failures across new and existing tests. Restored.
All three files confirmed byte-identical to the pre-mutation copies; full suite green afterwards.

## Findings (not fixed, per instructions; recorded as strict xfail so they flip loudly when fixed)
1. `test_bad_spend_cancelling_pair_not_hidden`: a raw-SQL negative PAID cost that is offset by a positive one (+5.0 and -5.0) nets to 0.0, so `build_router_state` accepts it. Only negative totals fail closed. The spec only requires totals, and `UsageRepository.record` rejects negatives, so this needs raw DB corruption; low severity. Suggest a per-row check (`MIN(estimated_cost_usd) < 0`) in `paid_spend`.
2. `test_corrupt_quota_row_fails_closed`: a raw-SQL EXACT snapshot with `remaining=-3` is returned by `latest()` and placed in `RouterState.quota` unvalidated (validation is write-side only, spec 6.2). Low severity, same class as above.

## Observations
- `install_redaction` skips any handler that already has some `RedactingFilter`, even one bound to a different redactor. This matches the spec wording and the CLI uses a single mutable redactor, so it is fine in-app. It did cause test pollution: `app.cli.main()` called in `test_cli.py` leaves a filter with an empty redactor on pytest's root capture handlers. My full-flow test therefore checks adapter-originated log records rather than `caplog.text` as a whole.
- Confirmed the coder's note: `latest()` newest-row-then-expiry semantics are unchanged and tested by the coder.
