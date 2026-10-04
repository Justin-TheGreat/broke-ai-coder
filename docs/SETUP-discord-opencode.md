# Setup: Discord bot + OpenCode in WSL Ubuntu

> For broke-ai-coder (round 3 prerequisites). Checked against OpenCode docs on 2026-10-01.
> Never paste a token or API key into Discord, Claude chat, Git, or `config.yaml`.
> Secrets live only in one WSL env file (Part C), loaded only for the controller's start command.

What you will end up with:

```text
WSL Ubuntu-24.04
├── ~/.config/broke-ai-coder/secrets.env   (chmod 600: bot token + provider keys)
├── opencode serve on 127.0.0.1:4096       (started and restarted by the controller, password-protected)
├── /workspace/                            (repos the agent may edit)
└── ~/broke-ai-coder/                      (the controller, cloned from GitHub)
Discord
└── private server "broke-ai-coder" with a bot that has slash-command access
```

---

## Part A — Discord (≈15 min, from a PC browser)

### A1. Create a private server
1. Discord → **+** (Add a Server) → **Create My Own** → **For me and my friends**.
2. Name it e.g. `broke-ai-coder`. Create a text channel `#agent`.

### A2. Turn on Developer Mode (needed to copy IDs)
- Desktop: **User Settings → Advanced → Developer Mode = on**.
- iPhone: **You (avatar) → Settings (gear) → Advanced → Developer Mode = on**.

### A3. Create the application and bot
1. Go to <https://discord.com/developers/applications> → **New Application** → name `broke-ai-coder` → accept the developer terms.
2. **General Information**: copy **Application ID** (not secret).
3. **Installation** tab (do this before turning Public Bot off, or Discord rejects it with
   "Private application cannot have a default authorization link"):
   - **Install Link** → **None** → Save Changes.
   - **Installation Contexts**: keep **Guild Install** ticked, untick **User Install** → Save Changes.
4. **Bot** tab:
   - Click **Reset Token** → copy the token **once** into a password manager. This is `DISCORD_BOT_TOKEN`. Anyone with it controls the bot; if it ever leaks, reset it here.
   - **Public Bot = off** (only you can invite it).
   - Privileged Gateway Intents: leave **all three off**. Slash commands do not need Message Content, Presence, or Server Members.

### A4. Invite the bot to your server
1. **OAuth2 → URL Generator**.
2. Scopes: `bot` and `applications.commands`.
3. Bot permissions (minimum): **View Channels, Send Messages, Send Messages in Threads, Embed Links, Attach Files, Read Message History, Use Slash Commands**. Do **not** grant Administrator.
4. Open the generated URL, pick your `broke-ai-coder` server, **Authorize**.
5. The bot appears offline in the member list — expected until the controller runs.

### A5. Collect the IDs (not secret — these go in `config.yaml`)
Right-click (desktop) or long-press (iPhone) → **Copy … ID**:

| Value | Where | Config key |
|---|---|---|
| Your user ID | your own name | `discord.allowed_user_ids` |
| Server ID | server icon | `discord.allowed_guild_ids` |
| Channel ID | `#agent` | `discord.allowed_channel_ids` |

---

## Part B — Provider API keys (≈15 min)

Create one key per provider. This project is **free-only**: the controller has no paid mode, rejects paid config, and only accepts OpenRouter `:free` models. But whether Gemini and Groq charge you is decided by **your account**, not by the code, so keep every account billing-free:

| Provider | Stay free by… | Why it matters |
|---|---|---|
| OpenRouter | **Never buy credits.** | With a $0 balance, a paid model can't be billed even if one were misconfigured. |
| Gemini (AI Studio) | Create the key in a Google Cloud project with **no Cloud Billing account linked**. In AI Studio the key should show the **Free** tier. | Keys in a billing-enabled project use the paid tier and are charged per token. |
| Groq | Stay on the **Free** plan; don't upgrade or add a card. | The paid plan bills usage above the free limits. |

With billing absent everywhere, hitting a limit just returns a 429 error and the router falls back to the next free provider, or stops.

| Provider | Where to create the key | Env var(s) |
|---|---|---|
| OpenRouter | <https://openrouter.ai/settings/keys> | `OPENROUTER_API_KEY` |
| Gemini (AI Studio) | <https://aistudio.google.com/apikey> | `GEMINI_API_KEY` only |
| Groq | <https://console.groq.com/keys> | `GROQ_API_KEY` |

These keys are for the **controller only**. Never give them to OpenCode: do not run `opencode auth login`, and do not export them in a shell where you run `opencode` yourself. The controller holds them in a loopback gateway; OpenCode only gets a random per-run token that unlocks your allowlisted free models on 127.0.0.1.

While in each dashboard, note the free-tier limits it shows (RPM/RPD/TPM). They go in `providers.<p>.limits` in `config.yaml` later; nothing is hard-coded in the app.

---

## Part C — WSL Ubuntu base setup (≈15 min)

All commands in this part run **inside Ubuntu**, not PowerShell.

### C1. Start Ubuntu
From PowerShell:

```powershell
wsl -d Ubuntu-24.04
```

### C2. Packages
Ubuntu 24.04 ships Python 3.12, which meets the project's 3.12+ requirement.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl sqlite3 tmux python3 python3-venv python3-pip gh
python3 --version   # expect 3.12.x
```

### C3. Git identity and GitHub login (inside WSL — separate from Windows)

```bash
git config --global user.name "Justin"
git config --global user.email "jichen9652@gmail.com"
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
gh api user --jq .login        # expect: Justin-TheGreat
```

### C4. Secrets file
Keep it outside any repo and readable only by you:

```bash
mkdir -p ~/.config/broke-ai-coder
touch ~/.config/broke-ai-coder/secrets.env
chmod 600 ~/.config/broke-ai-coder/secrets.env
nano ~/.config/broke-ai-coder/secrets.env
```

Contents (replace the placeholders; no quotes or spaces around `=`):

```bash
export DISCORD_BOT_TOKEN=paste-here
export OPENROUTER_API_KEY=paste-here
export GEMINI_API_KEY=paste-here
export GROQ_API_KEY=paste-here
```

Load it **only in the daemon's start command** (Part E), never in `~/.bashrc`. Check the file shows 4 lines and print **names only** (never print the values):

```bash
grep -c '^export ' ~/.config/broke-ai-coder/secrets.env     # expect 4
cut -d= -f1 ~/.config/broke-ai-coder/secrets.env             # names only
```

Start the controller with the file loaded for that one command (this is also what a tmux window or service unit should run):

```bash
cd ~/broke-ai-coder
bash -c 'set -a; . ~/.config/broke-ai-coder/secrets.env; set +a; exec .venv/bin/python -m app --config config.yaml'
```

**Residual risk R1.** Do **not** `source` `secrets.env` from `~/.bashrc`: every shell, including the agent's shell tool, would then carry your keys. Even so, today OpenCode runs as the same Linux user as the controller, so a deliberately rogue agent could still read `secrets.env` and `/proc/<controller pid>/environ`. This change keeps OpenCode's config and environment key-free; it is not yet a guarantee against a malicious agent. The planned fix (next round, T072) is to run OpenCode as a separate Linux user with no access to the secrets file or the controller's `/proc`. Ambient cloud credentials (`~/.aws`, gcloud ADC, Azure CLI, GitHub Copilot tokens) are reachable the same way through the allowed `HOME`/`XDG_*`, so do not keep any of them for the WSL user until T072. Until then, keep account-level billing off as the second layer (Part B).

### C5. Workspace for agent-editable repos
Keep repos on the Linux filesystem (not `/mnt/c/...` or OneDrive — slow and sync conflicts). This matches `opencode.working_directory: /workspace` in `config.example.yaml`:

```bash
sudo mkdir -p /workspace
sudo chown "$USER:$USER" /workspace
```

---

## Part D — OpenCode (≈10 min)

### D1. Install

```bash
curl -fsSL https://opencode.ai/install | bash
source ~/.bashrc          # installer adds opencode to PATH
opencode --version
```

(Alternative if the script fails: install Node.js LTS, then `npm install -g opencode-ai`.)

### D2. Give OpenCode no keys
OpenCode is given **no** provider keys and no stored credentials. The controller launches it with a minimal environment and an inline config whose only provider is the built-in gateway.

```bash
opencode auth list
```

- If this lists any provider, run `opencode auth logout` for **each** one. The daemon refuses to start while `~/.local/share/opencode/auth.json` (or `$XDG_DATA_HOME/opencode/auth.json`) holds any provider entry.
- Remove any `provider`, `model`, `small_model` or `enabled_providers` entries from `~/.config/opencode/opencode.json`. The controller sets them itself (D2b).
- Take the model ids for `config.yaml` (`routing.policies.*.model_order`) from each provider's dashboard or models API, **not** from `opencode models`, which no longer lists your providers. OpenRouter must be `openrouter/free` or end in `:free`.

### D2b. `model` and `small_model` are pinned by the controller
OpenCode makes small background calls (for example session title generation) with a separate `small_model`. The controller pins `model` and `small_model` itself through `OPENCODE_CONFIG_CONTENT` (optionally choose the small one with `gateway.small_model: "<provider>/<model>"` in `config.yaml`), together with `enabled_providers: ["broke"]`, so only the gateway provider can resolve. There is no manual edit.

Before every task the controller checks `GET /config` of its own server (exactly that gateway-only shape) and that no OpenCode credentials are stored. It also checks project files on disk: `opencode.json`/`opencode.jsonc` and `*.json`/`*.jsonc` directly inside `.opencode/`, in the project directory and every parent up to the filesystem root. Project files must not contain `provider`, `enabled_providers` or `disabled_providers`, and `model`, `small_model`, `agent.<name>.model`, `mode.<name>.model` and `command.<name>.model` must be absent or of the form `broke/<provider>/<model>` from your allowlist. Anything else (or an unparseable file) makes the task fail with `OPENCODE_UNSAFE_CONFIG`; nothing is run and no fallback happens.

### D3. Smoke test (uses free quota)
With no keys in OpenCode, a manual `opencode run` cannot work, so test through the controller:

```bash
mkdir -p /workspace/hello && cd /workspace/hello && git init -q
```

Start the daemon (Part E), then in Discord run `/code` on project `default` (`/workspace` in the example config) or, for `/workspace/hello`, add it under `opencode.projects` and use `/code project:hello`, asking for "Create hello.py that prints hello, then run it."

### D4. The controller runs `opencode serve` for you
Do **not** run `opencode serve` yourself. The daemon starts it on `127.0.0.1:<opencode.server_port>` (default 4096) with a random Basic-auth password, a daemon-owned working directory (`opencode.server_directory`), a key-free environment and the inline gateway config. It restarts it with backoff if it crashes. Stop any old tmux `opencode serve` session:

```bash
tmux kill-session -t opencode 2>/dev/null; pgrep -af 'opencode serve' || true
```

Security rules (SPEC §11):
- The server and the gateway bind to `127.0.0.1` only. Never port-forward them.
- The gateway token and server password are random per run and visible to the agent. That is acceptable: they only unlock allowlisted free models on localhost.
- `opencode.managed_server: false` (daemon does not start `serve`, uses `opencode.server_url` without a password) exists for tests and debugging only and is unsupported for production: an externally started server cannot pass the gateway-only preflight.

### D5. Keep it running
- Keep the controller itself running in a terminal or tmux window; it supervises `opencode serve`.
- Automatic start on reboot is task T102 and will be done in a later round (systemd user service or Task Scheduler).

---

## Part E — Clone the controller into WSL

The architecture runs the controller inside WSL next to OpenCode:

```bash
cd ~
gh repo clone Justin-TheGreat/broke-ai-coder
cd broke-ai-coder
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
cp config.example.yaml config.yaml     # config.yaml holds IDs only, never secrets
```

Edit `config.yaml`:
- `discord.allowed_user_ids / allowed_guild_ids / allowed_channel_ids` ← Part A5 (as strings or numbers per the config schema).
- `routing.policies.*.model_order` ← the model IDs from each provider's dashboard (D2).
- Optional `providers.<p>.limits` ← numbers from Part B dashboards.

Then:

```bash
.venv/bin/python -m app --check --config config.yaml
bash -c 'set -a; . ~/.config/broke-ai-coder/secrets.env; set +a; exec .venv/bin/python -m app --config config.yaml'
```

If startup fails the daemon prints one line and exits with status 3 (for example `OpenCode has stored provider credentials for: ...` or `opencode serve did not become healthy: ...`); see Troubleshooting.

---

## Checklist — send me these when done (no secrets)

- [ ] `opencode --version` output
- [ ] `opencode auth list` shows no providers (after `opencode auth logout`)
- [ ] Daemon started without a startup error; the log shows the gateway listening and no `did not become healthy`
- [ ] The WSL user holds no ambient cloud credentials (`~/.aws`, gcloud ADC, Azure CLI, Copilot tokens) until T072
- [ ] D3 smoke test worked (yes/no + any error text)
- [ ] Exact model IDs chosen per provider, in order
- [ ] Discord: bot invited to server (yes/no). IDs can stay in your local `config.yaml`; you don't need to send them.
- [ ] `python -m app --check` output from WSL

Do **not** send: the Discord bot token, any API key, or the contents of `secrets.env`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `opencode: command not found` | `source ~/.bashrc`, or open a new Ubuntu shell. The daemon prints `opencode executable not found` and exits 3 |
| `OpenCode has stored provider credentials for: ...` (exit 3, or task `OPENCODE_UNSAFE_CONFIG`) | Run `opencode auth logout` for each listed provider; OpenCode must hold no provider keys |
| `OpenCode credential store unreadable: <path>` | `auth.json` is not valid JSON or not readable; fix or delete it |
| `opencode serve did not become healthy: ...` (exit 3) | See the exit code and stderr tail in the message; check `opencode.command`, that `opencode.server_port` is not already used (stop any old tmux `opencode serve`), and `opencode.server_start_timeout_s` |
| `no enabled allowlisted models; check routing.provider_order/policies` | Enable at least one policy listed in `routing.provider_order` whose provider is enabled |
| Tasks fail with `OPENCODE_UNSAFE_CONFIG` | Remove `provider`/`enabled_providers`/`disabled_providers` and non-`broke/...` model keys from project `opencode.json` files, and run `opencode auth logout` |
| Gateway returns 503 `upstream_credential_missing` | The daemon was started without `secrets.env` loaded; use the start command in C4/E |
| 401 from a provider | Key copied wrong or revoked — regenerate in that dashboard, update `secrets.env` |
| 429 in smoke test | Free-tier limit hit — the gateway answers 429 and the router falls back to the next free provider |
| Health `curl` to 4096 returns 401 | Expected: the managed server needs its random password; check the controller log instead |
| Bot token leaked | Developer Portal → Bot → **Reset Token**, update `secrets.env` |
| Keys exported in `~/.bashrc` | Remove the `source ...secrets.env` line (residual risk R1) and open a new shell |
| Slash commands not visible on iPhone | Expected until round 3 registers them; then fully quit and reopen Discord |
