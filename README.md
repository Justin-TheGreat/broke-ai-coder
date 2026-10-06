# broke-ai-coder

A local agent controller that routes coding tasks across free-tier LLM providers
in a configured order, falling back on rate limits or quota exhaustion. It is
free-only: there is no paid mode, paid settings are rejected, and when all free
capacity is used up the task stops instead of paying.

## Quick start (WSL, production)

Each step links to a part of the [setup guide](docs/SETUP-discord-opencode.md).

1. Create the Discord application and bot ([Part A](docs/SETUP-discord-opencode.md#part-a--discord--15-min-from-a-pc-browser)).
2. Create the free provider API keys ([Part B](docs/SETUP-discord-opencode.md#part-b--provider-api-keys--15-min)).
3. Install the Ubuntu packages ([C2](docs/SETUP-discord-opencode.md#c2-packages)).
4. Install OpenCode ([D1](docs/SETUP-discord-opencode.md#d1-install)).
5. Clone the repo, create the venv and `config.yaml` ([E1](docs/SETUP-discord-opencode.md#e1-clone-install-configure)).
6. Run `sudo scripts/wsl-setup.sh --yes`, then `wsl --shutdown` in PowerShell and reopen Ubuntu.
7. Copy `secrets.env.example` to `~/.config/broke-ai-coder/secrets.env`, `chmod 600`, fill in the values ([C4](docs/SETUP-discord-opencode.md#c4-secrets-file)).
8. Run `scripts/live-checks.sh` and `scripts/health-check.sh`.
9. Start the daemon with `scripts/start-daemon.sh` (in tmux) or install the systemd unit ([Part F](docs/SETUP-discord-opencode.md#part-f--operate)).
10. Send your first `/code` ([D3](docs/SETUP-discord-opencode.md#d3-smoke-test-uses-free-quota)).
11. Try `/status`, `/usage`, `/models` and `/health`.
12. Read the security notes: SPEC section 11 and the OpenCode deny list ([D2c](docs/SETUP-discord-opencode.md#d2c-opencode-permissions-deny-list)).

## Development (Windows/tests)

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy config.example.yaml config.yaml
```

## Run

```powershell
.\.venv\Scripts\python -m app --check   # validate config and migrate the DB
.\.venv\Scripts\python -m app --once    # start, run one cleanup, stop (needs no opencode binary)
.\.venv\Scripts\python -m app           # run the daemon until Ctrl+C
```

## Test and lint

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

## Notes

- API keys are read only from the environment variables named in the config. Keys are
  never stored in config files or the database.
- HTTP provider adapters exist for metadata, health and quota only (no inference calls);
  inference goes through the built-in gateway.
- `providers.<p>.limits`: your own quota limits per dimension (optionally per model), used to
  estimate remaining quota from recorded usage. No limits are shipped.
- `providers.<p>.models`: per-model capability overrides (tools, vision, context window, ...).
- `cooldown`: how long rate-limited or failing providers and models are skipped.
- Log output redacts configured API keys.
- Production (WSL) runs OpenCode as a separate Linux user through sudo
  (`opencode.os_user`, setup guide Part C6); `--print-sudoers` and `--check-os-user` help with
  setup.
- On Windows/tests set `opencode.os_user.mode: none`, which is unsupported for production.
