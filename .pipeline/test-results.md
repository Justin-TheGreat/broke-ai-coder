# Test results: key-holding LLM gateway + managed `opencode serve` (feat/llm-gateway)

Status: ALL PASS. No application code was changed.

- `.venv/Scripts/python -m pytest -q`: 1143 passed, 1 xfailed (the pre-existing strict xfail).
- `ruff check .`: clean. `ruff format --check .`: clean (133 files).

## Tests added (the coder's suite was already broad; these fill gaps)

New files:
- `tests/test_gateway_hardening.py` (66 tests)
- `tests/test_opencode_hardening.py` (16 tests)
- `tests/fakes/fake_serve_tree.py` (wrapper that spawns a grandchild, then runs the fake serve; used for the tree-kill test)

Gateway coverage added:
- Auth: 11 token variants return 401 on both endpoints with zero upstream calls (lowercase or uppercase `bearer`, uppercased or lowercased token, truncated token, extra char, double space, `Basic`, empty). An oversized body with a bad token returns 401, not 413. `hmac.compare_digest` is called with bytes. A non-ASCII header returns 401, not 500. Over a real socket, a missing token gives 401 and a valid token with an oversized body gives 413.
- Look-alike model ids all return 403 with zero upstream calls and zero usage rows: case changes, whitespace, newline, NUL, trailing slash, double slash, query, unicode slash and hyphen, empty, `*`, bool, dict, and a missing `model`. Nothing is substituted when the key is missing (503, zero calls).
- Body whitelist: every one of the 21 allowed keys is forwarded and nothing else is. Dropped keys include `models`, `provider`, `plugins`, `route`, `transforms`, `extra_body`, `base_url`, `api_key`, `headers`, and `Model`/`MODELS` case variants. Dropped key values are never logged.
- 301, 302, 303, 307 and 308 each give 502 with exactly one upstream call, and `Location` and `Set-Cookie` are not relayed. Upstream response headers are not relayed. `Accept-Encoding: identity` is sent.
- Usage rows are FREE across success, 429, 500, 3xx and non-JSON bodies. Rejected requests (401, 403, 400, cooling) write no rows. `task_id` is read per request. Invalid token counts (negative, bool, string) give None.
- Secrets: no provider key or gateway token appears in any response body or header, in logs, or in usage rows across five scenarios. The real key appears only in the upstream `Authorization` header. A transport exception message that echoes the key and token is not leaked. The gateway token is scrubbed even when no redactor is configured.
- SSE: the usage parser finds usage for every chunk split (1, 2, 3, 7 and 1000 bytes), handles CRLF, keeps the last usage and ignores bad lines, and keeps its line buffer bounded when one line never ends. Also covered: stream requested but JSON returned, SSE returned without stream requested, and a multibyte payload split mid-character that stays byte-identical.
- Real socket: exactly one listening address, 127.0.0.1. A client disconnect mid-stream closes upstream and records UNKNOWN_OUTCOME with FREE.

Managed server / preflight coverage added:
- A parent `OPENCODE_CONFIG_CONTENT`, `OPENCODE_CONFIG_DIR`, `OPENCODE_SERVER_PASSWORD` and `FOO_API_KEY` never reach `serve`. The served inline config is the controller-generated one, with no provider key values in it.
- `stop()` kills the whole process tree (the grandchild pid is dead afterwards).
- Stored credentials in `auth.json` block `start()` with ids only, no values.
- The `run --attach` child env lacks `OPENCODE_CONFIG_CONTENT`, `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, and the provider keys, including the Google Generative AI one.
- End to end through the orchestrator and the real executor, all failing closed with `OPENCODE_UNSAFE_CONFIG`, no spawn and no attempt rows:
  - `auth.json` present, on every task, with zero `/config` calls;
  - an extra provider;
  - `enabled_providers` different;
  - a different baseURL;
  - a non-allowlisted model key;
  - `enabled_providers` missing.

## Mutation checks

Each was run against the full set of gateway, env, server, config-check and preflight tests. The file was backed up, mutated, tested, restored, and checked byte-exact with `filecmp` (identical in every case).

| Mutation | Result |
|---|---|
| gateway: drop `gid not in self.models` | RED |
| gateway: send the whole body (no `ALLOWED_REQUEST_KEYS` filter) | RED |
| env.py: filter replaced with `True` | RED |
| server.py: remove `enabled_providers` from the generated config | RED |
| server.py: skip the auth-store check at `start()` | RED |
| config_check.py: skip the auth-store problem | RED |
| gateway: `_authorized` always True | RED |
| gateway: read the body before the auth check | RED |
| gateway: `compare_digest` replaced by `==` | RED |
| gateway: `follow_redirects=True` | RED |
| gateway: skip the cooldown check | RED |
| gateway: remove the `_scrub` token replace | initially STILL GREEN (the suite's redactor already covered it); added `test_gateway_token_scrubbed_even_without_a_redactor`, now RED |
| config_check.py: drop the provider-key-set check | RED |
| gateway: bind `0.0.0.0` | RED |

No stray `.mutbak` files remain. No `fake_opencode` or `fake_serve_tree` processes remain.

## Notes (not failures)

- A leading or trailing space in the `Authorization` header is stripped by HTTP parsing before the app sees it, so those variants are not tested as 401.
- I did not test: upstream URL substitution by a client-supplied URL (the code has no such input), or a non-FREE usage class (`CostClass` has only FREE).
