# broke-ai-coder

A local agent controller that routes coding tasks across free-tier LLM providers
in a configured order, falling back on rate limits or quota exhaustion, and never
spending on paid models without explicit approval and a budget.

## Setup

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy config.example.yaml config.yaml
```

## Run

```powershell
.\.venv\Scripts\python -m app --check   # validate config and migrate the DB
.\.venv\Scripts\python -m app --once    # start, run one cleanup, stop
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
- HTTP provider adapters exist for metadata, health and quota only (no inference calls).
  Discord and OpenCode are not implemented yet.
- `providers.<p>.limits`: your own quota limits per dimension (optionally per model), used to
  estimate remaining quota from recorded usage. No limits are shipped.
- `providers.<p>.models`: per-model capability overrides (tools, vision, context window, ...).
- `cooldown`: how long rate-limited or failing providers and models are skipped.
- Log output redacts configured API keys.
