# Changes: R4 round

## app/opencode/executor.py
- Both `_abort_session` calls in `OpenCodeExecutor.run` (cancel/timeout path and shielded outer-CancelledError path) now pass `state.session_id or session_id`. A resumed session (`--session`) is aborted even before OpenCode prints its first event. `abort_opencode_session` still re-validates the id with fullmatch.
- Module docstring "Known gap" narrowed to brand-new sessions only.

## tests/test_opencode_abort.py
Four new tests, using `session_id="ses_known1"`:
- `hang_no_session` + cancel: exactly one POST to `/session/ses_known1/abort`.
- `hang_no_session` + timeout: same.
- `hang_no_session` + outer task cancellation: same.
- `hang` (stdout reports `ses_fake1`) + cancel: exactly one POST, to `/session/ses_fake1/abort` (observed id preferred).

## Verification
- `pytest -q`: 761 passed, 1 xfailed. `ruff check .` clean. `ruff format --check .` clean.
- Mutation check: reverting both calls to `state.session_id` makes the 3 `hang_no_session` resumed-session tests fail (28 passed, 3 failed in test_opencode_abort.py). The fix was restored afterwards.

## Tester focus
None beyond the above; nothing else was changed.
