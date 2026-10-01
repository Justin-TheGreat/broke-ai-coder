# Review: MVP vertical slice 1 (offline core)

VERDICT: NEEDS WORK

The slice matches the spec closely, and most of the safety invariants hold. pytest gives 161 passed, and ruff check and ruff format --check are both clean (I re-ran them). The work is blocked from SHIP by one safety-gate defect: with a $0 budget, the paid gate can still select a paid model. I reproduced this; details are below. The fix is small.

## Required fixes

1. **The paid budget gate fails open on NaN or negative spend. This breaks the "$0 budget never selects paid" rule.**
   `app/router/router.py:156-159` checks `daily_left <= 0`, which is a fail-closed test written the wrong way round. Reproduced in `free-first-paid-after-approval` mode with `daily_paid_budget_usd=0`, all free candidates unavailable, and `paid_approved=True`:
   - `paid_spend_today_usd=0.0` gives NoEligibleProvider. Correct.
   - `paid_spend_today_usd=float("nan")` gives **Selected(or-paid-1)**.
   - `paid_spend_today_usd=-1.0` gives **Selected(or-paid-1)**. A negative spend effectively raises the budget.

   `RouterState` (`app/router/types.py:43-51`) does not validate the spend fields. Spend will come from DB aggregation in the next slice, and refund or correction rows or a bad SUM would feed straight into this gate. Fix:
   - In `router.py:156-159`, write the gate positively so that anything not provably positive blocks paid:
     `if not (daily_left > 0) or (monthly is not None and not (monthly - state.paid_spend_month_usd > 0)):` -> `PAID_BUDGET_EXHAUSTED`.
   - In `types.py`, add a `RouterState.__post_init__` that raises `ValueError` unless both `paid_spend_*_usd` values are finite and `>= 0`. Use `math.isfinite`.
   - In `tests/test_router.py`, add a test that budget 0 with spend NaN or -1 and `paid_approved=True` never returns Selected PAID. Add the same test for the monthly cap.

2. **Reject non-finite budgets in config.** `app/config/models.py:52-53`: the YAML value `.inf` is currently accepted as `daily_paid_budget_usd` and means unlimited paid spend. I verified that `.inf` routes to Selected PAID. NaN is already rejected by `ge=0`. Add `allow_inf_nan=False` to both `Field(...)` calls, or add `allow_inf_nan=False` to `_CFG`. Then add `.inf` to the rejected cases in `tests/test_config.py`. SPEC §8 asks for a "configured hard daily/monthly budget", and infinity is not a hard cap.

3. **Make the config rejection tests check the actual error.** `tests/test_config.py:78` uses `pytest.raises(Exception)`, so it would pass on any `TypeError` or `KeyError` bug. `parse_config` wraps errors in `ConfigError`, so assert `pytest.raises(ConfigError, match=...)` with a per-case match string such as "unknown policy id", "duplicate", "undefined provider", "api_key_env", or "extra". Pass the match strings through the parametrize list.

## Should fix (test strength; not blocking on their own)

- `tests/test_router.py:144` `test_output_above_max_output` only checks for NoEligibleProvider. Also assert that the free skips are `SkipReason.CONTEXT_TOO_SMALL`. Today it would pass with any skip reason.
- `tests/test_router.py:66`: the "never a third Gemini model" case never puts both listed Gemini models in cooldown while the healthy unlisted `g-pro` is present. Add an assertion that the result is `c-a`, not `g-pro`, and that no candidate or skipped entry ever has model `g-pro`.
- `tests/test_router_fallback.py:105` `test_free_only_never_returns_paid` should also assert `reason == FREE_CAPACITY_EXHAUSTED` and that `or-paid-1` is skipped with `PAID_BLOCKED_BY_MODE`. Also run it with `paid_approved=True` and `paid_requires_approval: false` so the mode gate is the only thing blocking paid.
- `tests/test_daemon.py:66` is loose (`results[-1] is False`). All submissions happen synchronously before the worker can run, so assert the exact result `[True, False, False, False]`. Then yield once, let the worker take the item, and show that exactly one more submission is accepted and the next one is rejected.
- No test covers the skip-reason precedence (spec §5.2 "first failing check, in this exact order"). Add one candidate that fails several checks, for example excluded, disabled, and DOWN, and assert it gets `EXCLUDED_AFTER_FAILURE`. Add a second where PAID_BLOCKED_BY_MODE wins over PROVIDER_DOWN.
- No test covers model-scoped quota records. A record for `(gemini, g-3.8)` must not filter `g-3.7`.
- Retention: no test shows that an old provider_event or usage_event belonging to a SUCCEEDED task inside an OPEN session is kept. Old QUEUED and WAITING_APPROVAL tasks are not tested either; only RUNNING is.

## Verified correct (safety invariants)

- **Unlisted model never selected:** `build_candidates` (router.py:30-46) iterates only `provider_order` and then `model_order`. The capabilities map cannot inject candidates. `is_allowlisted` and `model_listing` are correct.
- **No reordering:** candidates come out in sort-key order and `route` only filters, preserving that order. Free is always chosen before paid.
- **Paid mode gate:** FREE_ONLY and FREE_FIRST_NO_PAID always skip PAID with PAID_BLOCKED_BY_MODE, whatever `paid_approved` is (router.py:86-90). The default config is no-paid with budget 0.
- **No silent FREE->PAID:** `next_after_failure` delegates to `route`, so fallback passes through the same mode, budget, and approval gate and returns PaidApprovalRequired (router.py:160-162). Selecting paid without approval only happens with an explicit `paid_requires_approval: false`, which matches SPEC §6.4 rule 7 ("unless policy explicitly allows it").
- **max_fallback_attempts:** enforced as total attempts, `len >= max` (router.py:175). The boundary is covered by tests: 2 attempts at the default still route, 3 stop.
- **AUTH_FAILED / PROVIDER_UNAVAILABLE** exclude the whole provider, including its paid policy. This is tested with paid otherwise auto-selectable, which is a good test.
- **Retention:** one cutoff per run, strict `<`, protected-task fragment reused, non-terminal tasks and OPEN sessions protected, batched deletes in per-batch transactions, checkpoint only after deletes, no VACUUM. The only non-bound SQL is the f-string with constant table names (retention.py:398).
- **SQL injection:** every value is bound with `?` or `:name`. The f-strings in tasks.py:315/327 interpolate only constant column fragments and placeholders.
- **Secrets:** only env var names are stored, enforced by `ENV_NAME_PATTERN`. Secret values are never logged or persisted, the schema has no key columns, and handler-error events log only the exception type name.
- **Bounded queues:** both queues are bounded, `put_nowait` rejects or drops instead of buffering, and there are no unbounded buffers.
- **Windows:** connections are closed in fixtures, in `stop()`, in the cleanup thread, and in `--check`. `add_signal_handler` NotImplementedError is caught.
- There is no network or subprocess use in `app/`.

## Notes / deferrals (acceptable for this slice)

- T052 lists "coding/tool reliability metadata". The spec deliberately covers only tool calling, structured output, vision, and context, so record this as a deferral in TASKS.md.
- `transaction()` (connection.py:29-38): if ROLLBACK itself raises (for example, SQLite already rolled back after SQLITE_FULL), that error hides the original one. Consider wrapping ROLLBACK in try/except. This is low priority.
- `submit_task` still accepts submissions after `stop()`. The queue is bounded, so nothing is lost silently, but rejecting submissions once `_stopped` is set would be cleaner.
