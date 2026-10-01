# Review: slice 2 (provider HTTP adapters, cooldowns, quota, redaction)

VERDICT: SHIP

Branch `feat/providers-quota` (uncommitted, intent-to-add on top of acb5844). I re-ran everything with `.venv/Scripts/python`:
- `pytest -q`: 635 passed, 2 xfailed (strict).
- pytest with `-X dev -W error::ResourceWarning -W error::pytest.PytestUnraisableExceptionWarning`: same result, no unclosed-client or unraisable warnings.
- `ruff check .`: clean. `ruff format --check .`: 83 files formatted.

The code matches spec slice 2 and TASKS T031-T035, T040-T043, T070 and T073 (the adapter-level parts that can be tested offline). I found nothing that must be fixed before SHIP. The follow-ups below are ranked. The first two must land before any slice that writes PAID usage or runs a quota refresh scheduler (T044/T054).

## Focus areas checked

### Secrets
- Keys are resolved per request (`http.py:177-178`) and sent only as per-request headers (`http.py:191`). They are never stored on the adapter, the error, the DB or an observation.
- Gemini uses `x-goog-api-key`, so no key goes in the URL. `follow_redirects=False`, so keys are never forwarded on a redirect.
- Transport errors are raised outside the `except` block (`http.py:193-203`), so both `__cause__` and `__context__` are None. This is stronger than the `from None` the spec asks for.
- Error messages and codes go through the redactor and through a replace of the resolved key, then are truncated (`http.py:244-256`).
- Adapter `repr` and ProviderError `repr` hold no key. `health_check` details contain only the HTTP status or the error class.
- `refresh_provider` logs only class names or ErrorClass values.
- RedactingFilter sits on the handlers. It redacts `exc_info`, `exc_text` and `stack_info`, and it flattens args.
- I probed it adversarially in the scratchpad: a 401 echoing the key in `message`, a 401 echoing it in `code`, and a 504 carrying a code. `str`, `repr` and the formatted traceback were all clean, with no cause and no context.

### No real network in tests
- The autouse guard patches `httpx.AsyncHTTPTransport.handle_async_request` (`tests/conftest.py`).
- Every adapter test uses MockTransport. The app has no sync httpx use.

### No hard-coded free-tier numbers
- Grepping `app/providers` and `app/quota` finds only the base URLs, the status maps, `GEMINI_MAX_PAGES` and the window lengths.
- Limits come only from `providers.<p>.limits`. `config.example.yaml` uses placeholders.

### Quota labelling
- A record is EXACT only when `remaining` was parsed from provider headers, or when OpenRouter `/key` returns a numeric limit and a numeric `limit_remaining`.
- Gemini returns `[]`. The estimator returns only ESTIMATED, or UNKNOWN with `remaining=None` when token counts are missing.
- `validate_record` rejects EXACT with an `estimate:` or `config:` source.
- Absent headers give `[]`, and missing dimensions are None, never 0.

### Cooldown scope
- RATE_LIMITED and QUOTA_EXHAUSTED are scoped to `(provider, model)`. PROVIDER_UNAVAILABLE and network failures over the threshold are scoped to `(provider, None)`. The later `until` wins, and `until == now` counts as expired.
- The router still walks `build_candidates` in `provider_order`/`model_order`, so a cooldown only skips. It never reorders.
- The tester mutated the 429 key to provider-wide and 9 tests failed.

### RouterState fails closed
- `capabilities` is re-filtered with `is_allowlisted` on both paths: observations and the DB fallback (`state.py:351-360`).
- `allowed_capabilities` re-checks against config, not the stored status. Unlisted and DISCOVERED_ONLY models never enter.
- Candidates come only from routing policies, so paid models still pass the cost-class, mode and budget gates.
- Spend is not clamped. A NULL cost raises RouterStateError (`state.py:375-376`). Negative or inf totals raise ValueError in `__post_init__`, which becomes RouterStateError, which becomes `ROUTER_STATE_UNAVAILABLE`.

### Error classification
- Connect-phase failures (ConnectError, ConnectTimeout, PoolTimeout, ProxyError) give TRANSIENT_NETWORK, which is safe to replay.
- Read, write and protocol errors and TimeoutError give TIMEOUT_UNKNOWN_OUTCOME, which is in STOP_ERRORS.
- Unclassified exceptions give UNKNOWN, which is also in STOP_ERRORS.
- 504 gives TIMEOUT_UNKNOWN_OUTCOME and 408 gives TRANSIENT_NETWORK.
- `classify_error` and `health_check` never raise.

### httpx cleanup
- The client is created lazily. `aclose` and `__aexit__` close it and reset it. Tests use `async with`, or call `aclose()` in a finally block.
- No production code builds adapters yet (T054), so no client is leaked in the app today.

### Tests
The tests are meaningful, not superficial:
- The end-to-end router-state test goes through real adapters over MockTransport into the DB and then the router.
- The tester ran 3 mutation checks (clamped spend, no exc_info redaction, provider-wide 429), and each one failed the suite as expected.

## The two strict-xfail findings: acceptable to defer, with a deadline

1. **`test_bad_spend_cancelling_pair_not_hidden`.** Raw-SQL rows of +5 and -5 net to 0.0.
   - Only DB corruption can cause this. `UsageRepository.record` rejects negative and non-finite costs (`usage.py:233-240`), and nothing else in `app/` writes `usage_events`. The implementation follows spec 7.3 to the letter.
   - It hides money, though, and spec section 11 says negative paid cost fails closed. **Fix it before any slice that records PAID usage**:
     - `app/db/usage.py:296-304`: add `SUM(CASE WHEN estimated_cost_usd < 0 THEN 1 ELSE 0 END)`.
     - Either return it in PaidSpend and raise in `state.py:375`, or count those rows as `missing_cost_events`.
   - Flip the xfail.
2. **`test_corrupt_quota_row_fails_closed`.** An EXACT row with `remaining=-3` (raw SQL) reaches RouterState unvalidated.
   - Routing is already fail-closed here. `router.py:121-124` treats `remaining < 1` as QUOTA_EXHAUSTED, and negative tokens as QUOTA_INSUFFICIENT.
   - NaN cannot be stored, because SQLite stores it as NULL and the router then ignores the row.
   - The cost is hygiene, not safety. Fix it in `app/db/quota.py:174` by running `validate_record` on each row read in `latest()` and letting the ValueError become RouterStateError (it is already caught at `state.py:337`). Flip the xfail.

## Follow-ups (carried forward, not blocking)

New from this slice:
- **Snapshot staleness, before T044.**
  - An EXACT snapshot with `reset_at=None` never expires (`db/quota.py:170`), and it suppresses the estimate forever (`estimator.py:128`).
  - A stale remaining of 0 can block a provider indefinitely, and a stale high remaining can hide exhaustion.
  - Add a max-age (config) in `latest()`, or in `_build`.
- `app/config/secrets.py:12-14`: `resolve_secret` returns the value unstripped. A key with a trailing newline or space from `.env` goes into the header as-is, and the per-key replace at `http.py:248` would then miss a bare echo. Strip it, and store the stripped form in the redactor.
- `app/providers/http.py:237-243`: the code map takes precedence over status. A 504 whose body says `rate_limit_exceeded` becomes RATE_LIMITED, which is replayable. For inference, never let a code map downgrade TIMEOUT_UNKNOWN_OUTCOME (a 504, or a read timeout). Revisit 500 and 502 as possible unknown-outcome for paid calls, too.
- `app/providers/ratelimit.py:103`: a huge reset header makes timedelta raise OverflowError, which drops the whole quota batch (`refresh_provider` catches it). Clamp `reset_s`, or treat it as None. Likewise, header ints above 2**63 make the insert raise OverflowError. Cap or drop them in `parse_number`.
- `app/providers/http.py:234-236`: `request_id` from response headers is neither truncated nor redacted. Truncate it to about 100 characters.
- `app/redaction.py:82-90`: `install_redaction` only covers handlers that exist at install time. Any handler added later (file, Discord, structured logs) must also get the filter. Add this to the T054 and Discord wiring checklist.
- `app/redaction.py:42-46`: partial key echoes (prefix or suffix) and schemes other than Bearer/Basic (for example `Token x`) are not redacted. This is acceptable now. Revisit it if a provider echoes long partial keys.
- `app/config/models.py:131-135`: `base_url` allows `http://`, which would send keys in cleartext. Warn, or reject it unless the host is localhost.
- **A9 assumption.** Groq and Cerebras record header quota from the `GET models` probe as provider-wide (`model=None`). Verify against real responses once network access is allowed. Groq limits are per model, so a provider-wide EXACT record can over-block.
- **Probe cost.** Groq and Cerebras make 3 `GET models` calls per refresh (health, list, quota). Reuse one response when T044 lands.
- **Adapter lifecycle in production (T054).** Whoever calls `build_adapters` owns the adapters and must `aclose()` them on shutdown.
- **Test hygiene.** `app.cli.main()` in `test_cli.py` leaves a RedactingFilter with an empty redactor on the pytest root handlers. Clean it up in that test, or make `install_redaction` replace filters bound to a different redactor.

Still open from slice 1 (unchanged):
- `test_router.py` `test_output_above_max_output`: assert `SkipReason.CONTEXT_TOO_SMALL`.
- "Never a third Gemini model": cool both listed Gemini models while `g-pro` is healthy, assert `c-a`, and assert `g-pro` is absent.
- `test_router_fallback.py` `test_free_only_never_returns_paid`:
  - Assert the reason.
  - Assert PAID_BLOCKED_BY_MODE.
  - Also run it with `paid_approved=True` and `paid_requires_approval: false`.
- `test_daemon.py:66`: assert exactly `[True, False, False, False]`, then show that exactly one more submission is accepted.
- Skip-reason precedence tests (spec slice-1 section 5.2).
- A model-scoped quota record for g-3.8 must not filter g-3.7.
- Retention: old events of a SUCCEEDED task inside an OPEN session are kept, and QUEUED and WAITING_APPROVAL tasks are protected.
- Record the T052 deferral in TASKS.md.

Closed this slice: the `transaction()` ROLLBACK guard (`db/connection.py:37-41`, tested), `submit_task` after stop (`runtime/daemon.py:150-152`, tested), and spend aggregation that surfaces as a clear error (RouterStateError, then ROUTER_STATE_UNAVAILABLE).
