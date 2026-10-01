# Review: MVP vertical slice 1 (offline core), re-review after fix round

VERDICT: SHIP

All 3 REQUIRED fixes from the previous review are done correctly, and the new tests can tell fixed code from broken code. I re-ran everything myself with `.venv/Scripts/python`. pytest gives 173 passed. `ruff check` passes and `ruff format --check` reports all 50 files formatted. The fix round touched only the files listed below, and no other behavior changed.

## Required fixes: verification

1. **Paid budget gate fails closed. DONE.**
   - `app/router/router.py:158-162`: the gate now reads `not (daily_left > 0) or (monthly is not None and not (monthly - spend_month > 0))`. A NaN in either term now gives PAID_BUDGET_EXHAUSTED.
   - `app/router/types.py:54-58`: `RouterState.__post_init__` rejects spend values that are non-finite or negative, using `math.isfinite`. This covers the -1 case, which the gate cannot catch by itself because 0 - (-1) > 0. RouterState is not built anywhere else in `app/` yet, so no callers break.
   - Tests in `tests/test_router.py`:
     - `test_router_state_rejects_bad_spend` checks NaN, inf and -1 for both fields, and asserts `ValueError` with the field name in the message.
     - `test_paid_gate_fails_closed_on_nan_spend` sets the other cap to 10.0, so only the NaN comparison can block paid. It skips RouterState validation by using `object.__setattr__`, which is the right way to test the gate directly.
     - I ran a positive control from the scratchpad: the same config and state with spend 0 returns `Selected` paid. The test therefore fails only because of the gate. This agrees with the tester's report that reverting the gate to `<= 0` makes it fail.
   - The original 4 cases with both budgets at 0 could not catch a revert. Replacing them with these 2 cases makes the suite stronger, even though the test count went down.
2. **Non-finite budgets are rejected in config. DONE.** `app/config/models.py:52-53` sets `allow_inf_nan=False` on both budget fields. Infinite daily and monthly budgets are covered in `test_invalid_rejected` (parse_config). They are also covered in `test_yaml_inf_budget_rejected`, which loads `.inf` from YAML through `load_config` and expects ConfigError. The default config still loads.
3. **Rejection tests now assert the actual error. DONE.** `tests/test_config.py:50-83`: every case uses `pytest.raises(ConfigError, match=...)` with its own message fragment, and the `noqa: B017` is gone.

## Follow-ups (carried forward, not blocking; fix in the next slice)

- `tests/test_router.py` `test_output_above_max_output`: assert that the free skips are `SkipReason.CONTEXT_TOO_SMALL`. Right now any skip reason passes.
- `tests/test_router.py` "never a third Gemini model": put both listed Gemini models in cooldown while the unlisted `g-pro` is healthy. Assert the result is `c-a` and that `g-pro` never shows up in candidates or skipped.
- `tests/test_router_fallback.py` `test_free_only_never_returns_paid`:
  - Assert `reason == FREE_CAPACITY_EXHAUSTED`.
  - Assert `or-paid-1` is skipped with `PAID_BLOCKED_BY_MODE`.
  - Also run it with `paid_approved=True` and `paid_requires_approval: false`.
- `tests/test_daemon.py:66`: assert exactly `[True, False, False, False]`. Then yield once and show that exactly one more submission is accepted.
- Add skip-reason precedence tests (spec section 5.2):
  - A candidate that is excluded, disabled and DOWN gets `EXCLUDED_AFTER_FAILURE`.
  - PAID_BLOCKED_BY_MODE wins over PROVIDER_DOWN.
- Add a test for model-scoped quota records: a record for `(gemini, g-3.8)` must not filter `g-3.7`.
- Retention tests:
  - Old provider_event and usage_event rows for a SUCCEEDED task inside an OPEN session are kept.
  - Old QUEUED and WAITING_APPROVAL tasks are protected. Only RUNNING is tested today.
- Record the deferral of T052 "coding/tool reliability metadata" in TASKS.md.
- `transaction()` (connection.py:29-38): wrap ROLLBACK in try/except so a failed rollback does not hide the original exception.
- `submit_task`: reject submissions once `_stopped` is set.
- When spend aggregation from the DB lands next slice, make sure the SUM is clamped or validated before it builds RouterState. `__post_init__` will raise on negative or NaN values, and that error has to surface as a clear error, not crash the router loop.
