# Test results: review fix round

Status: ALL PASS. pytest 173 passed; ruff check clean; ruff format --check clean (50 files).

## Regression check (paid gate fail-closed)
- The original fail-closed test (NaN/-1 with budgets 0) did NOT catch a revert: with the gate reverted to `<= 0`, 4/4 still passed, because the other zero-budget cap blocked paid. The -1 spend cases cannot be caught by the gate at all (0 - -1 = 1 > 0 in both versions); they are covered by RouterState validation.
- Fixed in tests/test_router.py only: replaced it with `test_paid_gate_fails_closed_on_nan_spend` (daily and monthly; the other cap is set to 10.0 so only the NaN comparison can block).
- With the gate reverted to `daily_left <= 0 or (monthly is not None and monthly - state.paid_spend_month_usd <= 0)`: 2 FAILED (as required). With the fix: 2 passed.
- router.py restored from a byte-exact backup; `git diff app/router/router.py` hash matches the pre-revert state.

## Other verified behaviors
- RouterState rejects NaN/inf/-1 for both spend fields (6 cases, `ValueError` match=field name).
- `.inf` daily/monthly budgets rejected via parse_config and via load_config from YAML (`.inf`), asserting ConfigError with match on the field name.
- Config rejection cases all assert `ConfigError` with a specific match.

Test count went 175 -> 173 because the 4 gate cases were replaced by 2 effective ones.
