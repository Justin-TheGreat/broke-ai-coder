# Setup: Discord bot + OpenCode in WSL Ubuntu

> For broke-ai-coder (round 3 prerequisites). Checked against OpenCode docs on 2026-10-01.
> Never paste a token or API key into Discord, Claude chat, Git, or `config.yaml`.
> Secrets live only in one WSL env file (Part C).

What you will end up with:

```text
WSL Ubuntu-24.04
├── ~/.config/broke-ai-coder/secrets.env   (chmod 600: bot token + provider keys)
├── opencode serve on 127.0.0.1:4096       (localhost only)
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
| Gemini (AI Studio) | <https://aistudio.google.com/apikey> | `GEMINI_API_KEY` and `GOOGLE_GENERATIVE_AI_API_KEY` (same value; the controller reads the first, OpenCode the second) |
| Groq | <https://console.groq.com/keys> | `GROQ_API_KEY` |

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
export GOOGLE_GENERATIVE_AI_API_KEY=paste-same-gemini-key-here
export GROQ_API_KEY=paste-here
```

Load it in every shell, then verify **names only** (never `echo` the values):

```bash
echo 'source ~/.config/broke-ai-coder/secrets.env' >> ~/.bashrc
source ~/.bashrc
env | grep -oE '^(DISCORD_BOT_TOKEN|OPENROUTER_API_KEY|GEMINI_API_KEY|GOOGLE_GENERATIVE_AI_API_KEY|GROQ_API_KEY)='
# expect 5 lines
```

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

### D2. Confirm OpenCode sees your providers
Because the keys are environment variables, OpenCode should detect them without `/connect`:

```bash
opencode auth list
opencode models openrouter | grep -i free
opencode models groq
opencode models google      # Gemini (AI Studio); if empty, see note below
```

- If a provider is missing, run `opencode auth login` and pick it (stored in `~/.local/share/opencode/auth.json`). Prefer the env-var route so the controller and OpenCode share one source.
- Gemini note: OpenCode's provider docs only list Vertex AI explicitly. If `opencode models google` shows nothing, run `opencode models --refresh` and check `opencode auth login` for "Google". Tell me what you see — the round-3 spec will adapt.
- **Write down the exact `provider/model` strings** you want for each allowlist (e.g. the two Gemini Flash IDs and two Groq). These replace the `<...>` placeholders in `config.yaml`.

### D3. One-off smoke test (proves keys + OpenCode work, uses free quota)

```bash
mkdir -p /workspace/hello && cd /workspace/hello && git init -q
opencode run --model groq/<a-groq-model-id-from-D2> "Create hello.py that prints hello, then run it."
ls; cat hello.py
```

### D4. Run the headless server (localhost only)

```bash
tmux new -s opencode
opencode serve --hostname 127.0.0.1 --port 4096
# detach with Ctrl+b then d; reattach with: tmux attach -t opencode
```

Check it from a second Ubuntu shell:

```bash
curl -s http://127.0.0.1:4096/global/health
# expect: {"healthy":true,"version":"..."}
cd /workspace/hello
opencode run --attach http://127.0.0.1:4096 --model groq/<same-model-id> "Add a docstring to hello.py"
```

Security rules (SPEC §11):
- Never use `--hostname 0.0.0.0` and never port-forward 4096 on your router.
- If you ever need it reachable beyond localhost (e.g. Tailscale), first set `OPENCODE_SERVER_PASSWORD` (and optionally `OPENCODE_SERVER_USERNAME`) in `secrets.env` to turn on HTTP Basic auth.

### D5. Keep it running
- WSL shuts down when no Windows terminal is attached and nothing is running; leaving the tmux session open in a terminal is enough for now.
- Automatic start on reboot is task T102 and will be done in a later round (systemd user service or Task Scheduler).

---

## Part E — Clone the controller into WSL

The architecture runs the controller inside WSL next to OpenCode:

```bash
cd ~
gh repo clone Justin-TheGreat/broke-ai-coder
cd broke-ai-coder
git checkout feat/providers-quota      # or main once branches are merged
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
cp config.example.yaml config.yaml     # config.yaml holds IDs only, never secrets
```

Edit `config.yaml`:
- `discord.allowed_user_ids / allowed_guild_ids / allowed_channel_ids` ← Part A5 (as strings or numbers per the config schema).
- `routing.policies.*.model_order` ← the model IDs from D2.
- Optional `providers.<p>.limits` ← numbers from Part B dashboards.

Then:

```bash
.venv/bin/python -m app --check --config config.yaml
```

---

## Checklist — send me these when done (no secrets)

- [ ] `opencode --version` output
- [ ] `curl http://127.0.0.1:4096/global/health` output
- [ ] D3 smoke test worked (yes/no + any error text)
- [ ] Exact model IDs chosen per provider, in order
- [ ] Whether `opencode models google` listed Gemini models
- [ ] Discord: bot invited to server (yes/no). IDs can stay in your local `config.yaml`; you don't need to send them.
- [ ] `python -m app --check` output from WSL

Do **not** send: the Discord bot token, any API key, or the contents of `secrets.env`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `opencode: command not found` | `source ~/.bashrc`, or open a new Ubuntu shell |
| Provider missing in `opencode auth list` | `source ~/.config/broke-ai-coder/secrets.env`; check var name spelling |
| 401 from a provider | Key copied wrong or revoked — regenerate in that dashboard, update `secrets.env` |
| 429 in smoke test | Free-tier limit hit — try another provider's model; this is what the router fallback handles |
| `curl` to 4096 refused | `tmux attach -t opencode` — server not running or crashed |
| Bot token leaked | Developer Portal → Bot → **Reset Token**, update `secrets.env` |
| Slash commands not visible on iPhone | Expected until round 3 registers them; then fully quit and reopen Discord |
