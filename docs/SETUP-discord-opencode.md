# Setup: Discord bot + OpenCode in WSL Ubuntu

> For broke-ai-coder (round 3 prerequisites). Checked against OpenCode docs on 2026-10-01.
> Never paste a token or API key into Discord, Claude chat, Git, or `config.yaml`.
> Secrets live only in one WSL env file (Part C), loaded only for the controller's start command.

What you will end up with:

```text
WSL Ubuntu-24.04
├── ~/.config/broke-ai-coder/secrets.env   (chmod 600: bot token + provider keys)
├── opencode serve on 127.0.0.1:4096       (started and restarted by the controller, password-protected,
│                                           run as the separate user brokeoc through sudo)
├── brokeoc                                (OpenCode user, own home /home/brokeoc, no access to your home)
├── /usr/local/bin/opencode                (root-owned copy of the opencode binary)
├── /var/lib/broke-ai-coder/opencode-server (serve's working directory, owned by brokeoc)
├── /workspace/                            (repos the agent may edit; group brokeai = you + brokeoc)
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

Do **not** `source` `secrets.env` from `~/.bashrc`: every shell, including the agent's shell tool, would then carry your keys. Residual risk R1 (OpenCode reading your secrets) is closed by Part C6: OpenCode runs as the separate Linux user `brokeoc`, which cannot read your home, `secrets.env` or the daemon's `/proc`. The remaining residuals (R1a-R1f) are listed in SPEC.md section 11. Keep account-level billing off as the second layer (Part B).

### C5. Workspace for agent-editable repos (shared with the OpenCode user)
Keep repos on the Linux filesystem (not `/mnt/c/...` or OneDrive — slow and sync conflicts). This matches `opencode.working_directory: /workspace` in `config.example.yaml`. The directory is shared through the group `brokeai` (you and `brokeoc`):

```bash
sudo apt install -y acl
sudo groupadd brokeai && sudo usermod -aG brokeai "$USER"
sudo mkdir -p /workspace
sudo chown "$USER:brokeai" /workspace
sudo chmod 2770 /workspace
sudo chgrp -R brokeai /workspace
sudo find /workspace -type d -exec chmod g+s {} +
sudo setfacl -R -m g:brokeai:rwX /workspace
sudo setfacl -R -d -m g:brokeai:rwX /workspace
```

Then log in again so that `id` shows `brokeai`: type `exit`, and in PowerShell run `wsl --terminate Ubuntu-24.04`, then reopen Ubuntu. Never put the controller repo (`~/broke-ai-coder`) in `/workspace`.

### C6. OpenCode user (one-time)
OpenCode runs as the unprivileged user `brokeoc` through `sudo -n -u brokeoc -- ...` with a narrow sudoers rule: you may run exactly four commands (the opencode binary, `pkill`, `test`, `head`) as `brokeoc`, never as root. `brokeoc` has its own home and cannot read yours.

1. Create the user and check its groups:
   ```bash
   sudo useradd --create-home --home-dir /home/brokeoc --shell /bin/bash --user-group brokeoc
   sudo usermod -aG brokeai brokeoc
   sudo chmod 700 /home/brokeoc
   id brokeoc          # must NOT list sudo, adm, docker or lxd
   ```
2. Lock down your own home and secrets:
   ```bash
   chmod 700 "$HOME" ~/.config/broke-ai-coder
   chmod 600 ~/.config/broke-ai-coder/secrets.env
   ```
3. Create the serve working directory:
   ```bash
   sudo install -d -m 0755 -o root -g root /var/lib/broke-ai-coder
   sudo install -d -m 2770 -o brokeoc -g brokeai /var/lib/broke-ai-coder/opencode-server
   ```
4. Close WSL interop and the default `/mnt/c` mount. With interop on, any Linux user (including `brokeoc`) could start a Windows `.exe` as your Windows account and read every Windows file and `\\wsl$` path; with the default DrvFs mount `/mnt/c` is world-readable. MERGE the following into `/etc/wsl.conf` with `sudo nano /etc/wsl.conf` and keep any existing entries such as `[boot]` `systemd=true`:
   ```ini
   [interop]
   enabled=false
   appendWindowsPath=false

   [automount]
   options="umask=077"
   ```
   Then in PowerShell run `wsl --shutdown` and reopen Ubuntu. **Cost:** you can no longer launch Windows programs from WSL shells (`code .`, `explorer.exe`, `clip.exe`, `powershell.exe`). The daemon's self-check refuses to start while interop is enabled.
5. Git for the agent (it gets no GitHub credentials). These one-time commands only write `brokeoc`'s git config, before the agent has ever run:
   ```bash
   sudo -u brokeoc git config --global user.name "broke-ai-coder agent"
   sudo -u brokeoc git config --global user.email "agent@example.invalid"
   sudo -u brokeoc git config --global --add safe.directory '*'
   ```
   **Never run git, tests, builds, or an editor/IDE git integration (VS Code Remote-WSL included) as yourself, or as the daemon user, inside `/workspace` once the agent has touched it.** The agent controls every repo's `.git/config` and `.git/hooks` (`core.fsmonitor`, `core.hooksPath`, `core.sshCommand`, `credential.helper`, filter/diff drivers, `include.path`, pre-commit and pre-push hooks). None of these show up in a diff, and even `git status` can run `core.fsmonitor`. That would be arbitrary code as you, with access to `secrets.env`.

   Safe review workflow: keep your own clone OUTSIDE `/workspace`, fetch the agent's branch into it, review there and push from there:
   ```bash
   cd ~/review/<repo>                                   # your own clone, not under /workspace
   git fetch file:///workspace/<repo> <branch>          # needs git >= 2.45.1 (local-clone CVE fixes)
   git diff HEAD...FETCH_HEAD; git log -p HEAD..FETCH_HEAD
   git checkout -b <branch> FETCH_HEAD && git push origin <branch>
   ```
   Check `git --version` first. Never run an agent-controlled program as `brokeoc` from your own terminal either: no `sudo -u brokeoc bash`, no interactive shell, no `opencode`/`git` as `brokeoc` (they read agent-writable dotfiles, config and plugins). Use the non-tty form shown in E3 so the process gets no controlling terminal.
6. The sudoers rule is installed in Part E (E2), after the clone, the venv and `config.yaml` exist and `/usr/local/bin/opencode` has been installed (D1). Do not skip it: nothing works without it.

Notes:
- The daemon kills ALL processes of `brokeoc` at start, at stop and on every serve restart. Do not use `brokeoc` for anything else.
- sudo logs each command line (including prompts) to the system journal / `auth.log`.
- On Windows (tests/dev) set `opencode.os_user.mode: none`. That runs OpenCode as the daemon user and is unsupported for production.

---

## Part D — OpenCode (≈10 min)

### D1. Install

```bash
curl -fsSL https://opencode.ai/install | bash
source ~/.bashrc          # installer adds opencode to PATH
opencode --version
sudo install -m 0755 -o root -g root "$(command -v opencode)" /usr/local/bin/opencode
```

(Alternative if the script fails: install Node.js LTS, then `npm install -g opencode-ai`.)

`brokeoc` must not be able to modify the binary, so the daemon uses a root-owned copy at `/usr/local/bin/opencode` (the `opencode.command` default in `config.example.yaml`). Re-run the `install` line after every OpenCode upgrade. If you installed with npm, point `opencode.command` at the npm global binary path instead and regenerate the sudoers file (E2).

### D2. Give OpenCode no keys
OpenCode is given **no** provider keys and no stored credentials. The controller launches it with a minimal environment and an inline config whose only provider is the built-in gateway. Checking and clearing `brokeoc`'s stored credentials needs the sudoers rule, so it is step E3 (after E2).

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
Do **not** run `opencode serve` yourself. The daemon starts it as `brokeoc` (through sudo, Part C6) on `127.0.0.1:<opencode.server_port>` (default 4096) with a random Basic-auth password, a daemon-owned working directory (`opencode.server_directory`), a key-free environment and the inline gateway config. It restarts it with backoff if it crashes. Stop any old tmux `opencode serve` session:

```bash
tmux kill-session -t opencode 2>/dev/null; pgrep -af 'opencode serve' || true
pgrep -u brokeoc -a          # inspect what runs as the OpenCode user
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

The architecture runs the controller inside WSL next to OpenCode. Do D1 first (it creates `/usr/local/bin/opencode`), then follow E1 to E4 in order.

### E1. Clone, install, configure

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

```bash
.venv/bin/python -m app --check --config config.yaml
```

### E2. Install the sudoers rule and verify (one-time; needs C6 steps 1-5 and D1)
Never use `/tmp` for the temporary file, because `brokeoc` can write there. The subshell with `set -e` stops at the first error, and the `-s` test refuses an empty file (which `visudo -cf` would accept):

```bash
cd ~/broke-ai-coder
(
  set -e
  tmp=$(mktemp ~/broke-ai-coder.sudoers.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  .venv/bin/python -m app --print-sudoers --config config.yaml > "$tmp"
  [ -s "$tmp" ]
  sudo visudo -cf "$tmp"
  sudo install -m 0440 -o root -g root "$tmp" /etc/sudoers.d/broke-ai-coder
  sudo visudo -c
)
```

The file name has no dot, so sudo reads it. This needs `opencode.command[0]` to be `/usr/local/bin/opencode` (D1) in your `config.yaml`; regenerate the file if you change it. The generated file does not touch `use_pty`: leave Ubuntu's default `Defaults use_pty` alone.

Verify (non-tty form for anything that runs as `brokeoc`, see the note in C6 step 5):

```bash
sudo -k
setsid -w sudo -n -u brokeoc -- /usr/local/bin/opencode --version </dev/null 2>&1 | cat   # works, no password prompt
sudo -n true                                                                              # must FAIL (no root)
setsid -w sudo -n -u brokeoc -- /usr/bin/test -r ~/.config/broke-ai-coder/secrets.env </dev/null; echo $?   # expect 1
.venv/bin/python -m app --check-os-user --config config.yaml                              # expect: os_user OK
```

### E3. Check that OpenCode holds no credentials (this is D2)
OpenCode runs as `brokeoc`, so check there. Never open a shell as `brokeoc` and never run these from your terminal without `setsid -w ... </dev/null | cat` (no controlling tty):

```bash
setsid -w sudo -n -u brokeoc -- /usr/local/bin/opencode auth list </dev/null 2>&1 | cat
```

- If this lists any provider, delete the credential store (`rm` is not agent-controlled, unlike `opencode auth logout`, which loads agent-writable config and plugins):
  ```bash
  sudo -u brokeoc rm -f /home/brokeoc/.local/share/opencode/auth.json
  ```
  This is a plain `sudo` (it asks for your password; the narrow sudoers rule only covers the four daemon commands). The daemon refuses to start while that file holds any provider entry.

### E4. First start

```bash
bash -c 'set -a; . ~/.config/broke-ai-coder/secrets.env; set +a; exec .venv/bin/python -m app --config config.yaml'
```

If startup fails the daemon prints one line and exits with status 3 (for example `OpenCode has stored provider credentials for: ...` or `opencode serve did not become healthy: ...`); see Troubleshooting.

---

## Checklist — send me these when done (no secrets)

- [ ] `opencode --version` output
- [ ] `setsid -w sudo -n -u brokeoc -- /usr/local/bin/opencode auth list </dev/null 2>&1 | cat` shows no providers (after deleting `/home/brokeoc/.local/share/opencode/auth.json` with `sudo -u brokeoc rm -f`, E3)
- [ ] `python -m app --check-os-user --config config.yaml` output (expect `os_user OK`)
- [ ] Daemon started without a startup error; the log shows the gateway listening and no `did not become healthy`
- [ ] `brokeoc`'s home holds no cloud credentials (`~/.aws`, gcloud ADC, Azure CLI, Copilot tokens)
- [ ] D3 smoke test worked (yes/no + any error text)
- [ ] Exact model IDs chosen per provider, in order
- [ ] Discord: bot invited to server (yes/no). IDs can stay in your local `config.yaml`; you don't need to send them.
- [ ] `python -m app --check` output from WSL

Do **not** send: the Discord bot token, any API key, or the contents of `secrets.env`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `opencode: command not found` | `source ~/.bashrc`, or open a new Ubuntu shell. The daemon prints `opencode executable not found` and exits 3 |
| `OpenCode has stored provider credentials for: ...` (exit 3, or task `OPENCODE_UNSAFE_CONFIG`) | Delete the store: `sudo -u brokeoc rm -f /home/brokeoc/.local/share/opencode/auth.json` (E3; do not run `opencode auth logout` as `brokeoc` from your terminal); OpenCode must hold no provider keys |
| `OpenCode credential store unreadable: <path>` | `auth.json` is not valid JSON or not readable; fix or delete it |
| `opencode serve did not become healthy: ...` (exit 3) | See the exit code and stderr tail in the message; check `opencode.command`, that `opencode.server_port` is not already used (stop any old tmux `opencode serve`), and `opencode.server_start_timeout_s` |
| `no enabled allowlisted models; check routing.provider_order/policies` | Enable at least one policy listed in `routing.provider_order` whose provider is enabled |
| Tasks fail with `OPENCODE_UNSAFE_CONFIG` | Remove `provider`/`enabled_providers`/`disabled_providers` and non-`broke/...` model keys from project `opencode.json` files, and delete `brokeoc`'s `auth.json` (E3) |
| `a password is required` in the `--version` check | The sudoers file is not installed, or names the wrong user or binary path; regenerate it (E2) |
| `user 'brokeoc' can access <path>` | `chmod` per C6 (home 700, `secrets.env` 600) and the `[automount] options="umask=077"` entry in `/etc/wsl.conf` |
| `WSL interop is enabled` | C6 step 4, then `wsl --shutdown` |
| `cannot write opencode.server_directory` | C6 step 3 (owner `brokeoc`, group `brokeai`, mode 2770) |
| `does not enforce the password ... env_keep` | Reinstall the generated sudoers file (E2); its `env_keep` line is missing |
| `will not restart by itself ... restart the daemon` | Stop whatever holds the serve port (`opencode.server_port`), then restart the daemon |
| `mode 'sudo' needs Linux` | You are on Windows (dev): set `opencode.os_user.mode: none` |
| Gateway returns 503 `upstream_credential_missing` | The daemon was started without `secrets.env` loaded; use the start command in C4/E |
| 401 from a provider | Key copied wrong or revoked — regenerate in that dashboard, update `secrets.env` |
| 429 in smoke test | Free-tier limit hit — the gateway answers 429 and the router falls back to the next free provider |
| Health `curl` to 4096 returns 401 | Expected: the managed server needs its random password; check the controller log instead |
| Bot token leaked | Developer Portal → Bot → **Reset Token**, update `secrets.env` |
| Keys exported in `~/.bashrc` | Remove the `source ...secrets.env` line and open a new shell |
| Slash commands not visible on iPhone | Expected until round 3 registers them; then fully quit and reopen Discord |
