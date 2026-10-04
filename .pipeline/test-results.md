# Test results: F1 fix round (A, B, F18)

Status: ALL PASS.

- Full pytest: 929 passed, 1 xfailed. ruff check clean. ruff format --check clean (119 files).
- Tests added to tests/test_project_config_check.py (now 41 tests): JSONC with https:// URLs
  (parses, and a bad model key after a URL is still UNSAFE), detail names file+key path never the
  value, child env strips DISCORD_BOT_TOKEN but keeps provider keys, UNSAFE project config leaves
  zero fallback attempts and zero cooldowns and never spawns, agent-written opencode.json between
  two tasks fails the second, allowlist excludes a post-load paid mapping (defense in depth).

## Mutation checks (each restored byte-exact, verified with cmp)
- A: `mapped not in _FREE_PROVIDERS` -> `False` in allowed_small_models: initially SURVIVED (load
  check masked it); added the post-load mutation test, now FAILS. Dropping the load check in
  models.py: test_unmapped_provider_rejected_at_load FAILS.
- F18: removed `_bad_model_key(body, allowed)` call: test_config_response_other_model_keys_unsafe FAILS.
- B: `_scope_dirs` project-only: test_parent_dirs_up_to_git_root_checked_not_above FAILS.
- B: orchestrator skips project check result: 3 tests FAIL (never_spawns, no_attempt_no_cooldown, between_tasks).
- B: no pop of OPENCODE_CONFIG_CONTENT: test_child_env_strips_config_injection_vars FAILS.
