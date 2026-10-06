# Changes: review fixes (R-1, R-2 and all recommended items)

Nothing committed. Branch feat/go-live. scripts/*.sh keep mode 100755 and LF (no CR bytes). Nothing was run against the real WSL, sudo or network; the guards were exercised in Git Bash with stubbed filesystem helpers.

## R-1 (blocker): wsl-setup.sh trusted config paths while root
- `scripts/wsl-setup.sh`: new guard block before Phase A (functions `canon_path`, `path_kind`, `owner_mode`, `under`, `check_root_dir`, `valid_home`, `check_apart`). Phase A now calls it for SERVER_DIRECTORY and WORKING_DIRECTORY; every refusal goes through `problem`, so nothing is changed. It replaces the old `-L WORKING_DIRECTORY` and "under $H" prefix checks.
  1. Normalization: absolute, and `realpath -m` must equal the configured value (rejects `..`, `.`, `//`, trailing `/`, symlinked components).
  2. Refuses `/` and the path or anything inside: /bin /boot /dev /etc /lib /lib32 /lib64 /libx32 /opt /proc /root /run /sbin /snap /srv /sys /usr /tmp /var/tmp /mnt /home /media. `/var` and everything under it is refused except strictly below `/var/lib/broke-ai-coder/`. The exact list is justified in a comment. Defaults `/workspace` and `/var/lib/broke-ai-coder/opencode-server` pass.
  3. A symlink, or an existing non-directory, is refused (both paths).
  4. Every existing ancestor (not only the nearest) must be owned by root and not group/other-writable (`stat -c '%U %a'`, last two digits, so 1777 is refused).
  5. SERVER and WORKING may not equal/contain each other; neither may equal/contain the repo, `dirname $DATABASE_PATH`, the daemon user's home, or `/home/$OC_USER`.
- `app/ops/setup_values.py`: `fullmatch` instead of `match` (trailing newline); SERVER_DIRECTORY and WORKING_DIRECTORY must be normalized absolute (`is_normalized_absolute`), exit 2 otherwise.
- `app/opencode/os_user.py`: `_under` compares `posixpath.normpath` values.
- Tests: new `tests/test_wsl_setup_guards.py` (runs the real guard functions in bash with stubs) plus additions in `tests/test_ops.py` and updated needles in `tests/test_scripts.py`. Rejection cases include the reviewer's `/tmp/../home/jichen`, `/var/lib/../../etc`, `/usr/local`, `/`, `/etc`, server dir inside working dir, symlinked server dir and symlinked component, unsafe ancestors (owner, 775, 757, 777, 1777), DB dir/repo/home containment (static), and the defaults pass.

## R-2
- `docs/SETUP-discord-opencode.md` C2: `acl` added to the apt line; git-core PPA commands added right there (printed for the user), with `git --version` check.
- README step 3 lists `acl` and the git-core PPA. wsl-setup.sh precondition message: `sudo apt install acl`.

## Recommended (all applied)
- `$H` validated by `valid_home` (absolute, normalized, existing directory, not a symlink, not `/`, owned by `$U`) in Phase A, before step 4's `chmod 700`.
- OpenCode user home: passwd home must equal `/home/<name>` (Phase A problem otherwise); Phase B uses `$OC_HOME` everywhere.
- git identity: runs as `(cd / && ...)`, no `| cat`, each call `|| die`, and `.gitconfig` must exist afterwards.
- sudoers install is atomic: install to `/etc/sudoers.d/.broke-ai-coder.new` (dotted, ignored by sudo), `visudo -cf` it, then `mv -f`; rollback uses the same path; "up to date" also requires `root 440`; the temp name is removed by the EXIT trap (root only).
- Empty merged wsl.conf refused (`die`) before the install.
- ERR trap (`set -o errtrace`) reports the line and the STEP; Phase A failures say "nothing was changed", Phase B failures say re-running is safe. Exit code 1 is documented for post-change failures.
- `scripts/live-checks.sh`: `sudo -n -k true` for L1; L0 git message points at setup guide C2.
- README anchors fixed to `#part-a--discord-15-min-from-a-pc-browser` and `#part-b--provider-api-keys-15-min`.
- `scripts/start-daemon.sh`: a relative `--config` is made absolute from the caller's directory before use.
- `TASKS.md` T072 status: "DONE (offline; live check in T082)". L13 and guide Part F note that `network-online.target` can stall WSL boot (`systemd-networkd-wait-online`) with the remedy; the unit file itself is unchanged (reviewer only asked for a note).

## Mutation checks (each applied alone to the working tree, targeted suites run, then restored)
KILLED: realpath-equality check; `/` refusal; forbidden-tree loop; `/var` rule; symlink refusal; ancestor owner/mode check; `check_apart` body; `check_apart` calls for DB dir, repo and server-vs-working; both `check_root_dir` calls; OC home passwd check; `valid_home` call (after adding a static assertion; first run survived), and two of its internal checks; `sudo -n -k` reverted to `sudo -n`; atomic install reverted to writing the final path; `_under` normpath removed; `fullmatch` reverted to `match`; the setup_values normalization check removed.

## Focus for the Tester
- Guards are tested with stubbed `canon_path`/`path_kind`/`owner_mode`; real `realpath -m` behaviour on Linux was not run (Git Bash maps `/tmp` oddly), so confirm `realpath -m` gives the input unchanged for `/workspace` and `/var/lib/broke-ai-coder/opencode-server` on real WSL (read-only: `realpath -m /workspace`).
- The DB-dir rule refuses a database placed in `/var/lib/broke-ai-coder` itself (it contains the serve directory); the default `data/` under the repo is fine.
- Phase B and the sudoers/git changes have still never been executed.
