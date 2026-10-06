# Test results: R-1 / R-2 fix round

RESULT: ALL PASS

- `timeout 500 pytest -q`: 1712 passed, 5 skipped (shellcheck absent), 1 xfailed.
- `ruff check .` clean; `ruff format --check app tests scripts` clean (138 files); `bash -n` OK on all scripts.
- scripts/*.sh: no CR bytes, `git diff --summary` shows no mode change (index stays 100755). No fake_opencode/fake_serve left.

## Added (tests/test_wsl_setup_guards.py, now 95 tests)
- Executable containment rules: the Phase A block (server vs working, repo, DB dir, daemon home, OpenCode home) is extracted and run with the same stubs. Defaults pass; 14 equal/inside/contains cases are rejected; DB dir containing the serve dir is rejected; daemon-home check skipped when USER_OK=0.
- README guide links: GitHub-style anchors are computed from the guide headings and every README link must resolve (Part A/B anchors confirmed).
- ERR trap runs for real: message has line, step, and "nothing was changed" (Phase A) vs "re-running is safe" (Phase B).
- Already covered by the coder and re-verified: non-normalized paths, `/`, every denylisted tree, /var rule, symlink and non-directory, unsafe ancestors (owner, 775, 757, 777, 1777), valid_home, OC home check, git identity, sudoers atomic/mode, empty wsl.conf, `sudo -n -k true`, relative `--config`, docs C2 (acl + git PPA).
- Lead's real-WSL facts (realpath -m identity for defaults, setfacl missing) are consistent with the stub model; not re-run.

## Mutation checks (each applied alone, restored, restored file identical via cmp, no CR, mode untouched)
All KILLED: realpath equality (6 failing tests), denylist loop (23), symlink refusal (1), ancestor check (8), check_apart repo (3), db (2), OpenCode home (3), daemon home (3), server-vs-working (17), `sudo -k` (1), atomic install reverted (1), sudoers owner/mode check (1).

## Incident (self-inflicted, resolved)
My first mutation driver crashed before restoring (bad python path), so mutations accumulated in scripts/wsl-setup.sh and scripts/live-checks.sh. I reverted them by hand (no original backup existed). Result verified: the guard and containment blocks match the earlier read, the suite and static tests pass, and the diff stat is unchanged. The lead may want a quick `git diff scripts/wsl-setup.sh scripts/live-checks.sh` glance to confirm.
