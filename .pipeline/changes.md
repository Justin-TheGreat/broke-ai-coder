# Changes: F1 review fixes (A, B, F18)

## Fix A: allowlist scope
- `app/config/models.py`: `AppConfig` validation rejects any routing policy whose provider is not mapped in `opencode.provider_map` (defaults included) to openrouter/google/groq.
- `app/opencode/config_check.py` `allowed_small_models`: only enabled policies on enabled providers mapped to those three.

## Fix B: project-scope on-disk check (B13)
- `config_check.py`: `strip_jsonc`, `check_project_config(_async)`. Scans project dir and parents up to and including the git root (project dir only if no `.git`): `opencode.json`, `opencode.jsonc`, `.opencode/*.json|jsonc`. Unparseable, non-object or non-readable files are UNSAFE. Detail names file path and key path only.
- `executor.py`: new `check_project_models(allowed, project_dir)`; `_child_env` strips `OPENCODE_CONFIG` and `OPENCODE_CONFIG_CONTENT`.
- `interfaces.py`: `AgentExecutor.check_project_models`. `tests/fakes/fake_executor.py` implements it (`project_check`, `project_calls`).
- `orchestrator.py`: after the existing `/config` check, runs the project check every task (uncached); UNSAFE gives `OPENCODE_UNSAFE_CONFIG` with `config_detail` field; an exception also fails closed.
- `app/bot/format.py`: unsafe text mentions global small_model and project model keys, and appends "Offending: <file: key>".

## F18
- `_bad_model_key` is applied to the `GET /config` response: `model`, `agent.*.model`, `mode.*.model` optional but allowlisted if present; `small_model` still required.

## Docs/spec
- `docs/SETUP-discord-opencode.md` D2b (global file, project rules, env stripping), `config.example.yaml` comment, `.pipeline/spec.md` B12 update + new B13.

## Tests
- New `tests/test_project_config_check.py` (anthropic repro, disabled policy/provider, JSONC, per-project isolation, `.opencode/*`, git-root bounds, unparseable, F18 keys, child env, orchestrator never spawns).
- `tests/test_config.py::_data` now maps providers a/b (needed by the new load rule).
- Results: 922 passed, 1 xfailed; ruff check/format clean; `-m app --check` OK.

## Suggested mutation checks
- A: in `allowed_small_models` replace `mapped not in _FREE_PROVIDERS` with `False` (or drop the load-time check in `models.py`): the anthropic and disabled-provider tests must fail.
- B: make `_scope_dirs` return `[start]` only, or have the orchestrator skip `check_project_models`: the parent/git-root and orchestrator-never-spawns tests must fail. Also remove the `pop` of `OPENCODE_CONFIG_CONTENT`: the child env test must fail.
- F18: delete the `_bad_model_key(body, allowed)` call in `check_small_model`: `test_config_response_other_model_keys_unsafe` must fail.
