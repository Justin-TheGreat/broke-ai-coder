# Changes: review fix round (3 REQUIRED fixes)

Results: pytest 175 passed; ruff check clean; ruff format --check clean.

## Files
- app/router/router.py: paid budget gate now written positively, `not (daily_left > 0) or (monthly is not None and not (monthly - month_spend > 0))`, so NaN or negative values block paid (PAID_BUDGET_EXHAUSTED).
- app/router/types.py: `RouterState.__post_init__` raises ValueError if `paid_spend_today_usd` or `paid_spend_month_usd` is non-finite or negative (added `import math`).
- app/config/models.py: `allow_inf_nan=False` on `daily_paid_budget_usd` and `monthly_paid_budget_usd`.
- tests/test_router.py: new tests: RouterState rejects NaN/inf/-1 spend for both fields; router gate fails closed on NaN/-1 for both daily and monthly (state mutated via `object.__setattr__` to bypass validation, budgets 0, paid_approved=True).
- tests/test_config.py: `test_invalid_rejected` now takes `(data, match)` and asserts `pytest.raises(ConfigError, match=...)` per case; added inf daily/monthly cases; new `test_yaml_inf_budget_rejected` loading `.inf` from YAML.

## Tester focus
- Confirm the object.__setattr__ bypass test fails if the router gate is reverted to `<= 0`.
- Confirm `.inf` budget is rejected via load_config with ConfigError.
- No other behavior changed; "Should fix" review items were not touched.
