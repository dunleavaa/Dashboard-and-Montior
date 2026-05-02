# NT Monitor

Lightweight monitoring for NinjaTrader 8 accounts and strategies. Posts
status, fills, and alerts to Discord and serves a phone-friendly dashboard
on your local network.

## What it does

- Watches NT8 from a separate machine — zero load on your trading PC's
  network or CPU beyond a handful of microseconds writing JSON to disk.
- Pinned, always-current status message in Discord showing connected
  accounts, balances, enabled strategies, and open positions.
- Fill notifications posted to a separate Discord channel as they happen.
- Alerts when something goes wrong: NT crashes, a strategy gets disabled,
  an account disconnects, or the monitor itself stops updating.
- Local web dashboard (`http://[machine-ip]:8080/`) with per-symbol P&L
  computed from your fills, broken down by account.

## How it works

```
┌────────────────────┐         ┌─────────────────────────┐
│  NT8 Machine       │         │  Python Machine         │
│  (Windows)         │         │  (Windows or Linux)     │
│                    │         │                         │
│  NinjaTrader 8     │         │  service.py             │
│   ↓                │         │   ├─ reads JSON files   │
│  MonitorExporter   │  SMB    │   ├─ tracks per-symbol  │
│  strategy          ├────────►│   │   P&L (FIFO match)  │
│   ↓                │ (read-  │   ├─ posts to Discord   │
│  C:\NT_Logger\     │  only)  │   └─ serves dashboard   │
│   ├─ heartbeat.json│         │                         │
│   ├─ strategies.   │         │                         │
│   │   json         │         │                         │
│   └─ executions.log│         │                         │
└────────────────────┘         └────────────┬────────────┘
                                            │
                                            ▼
                              Discord channels + http://lan-ip:8080
```

NT8 writes three small JSON files every 30 seconds (and on every fill).
The Python service reads those files over a network share, computes
session-relative P&L, posts to Discord webhooks, edits a pinned status
message via the bot API, and serves a simple HTML dashboard.

## Requirements

**NT8 machine:**
- NinjaTrader 8
- A folder you can share read-only over SMB (`C:\NT_Logger` by default)

**Python machine:** (can be the same machine, but a separate one is more
robust if NT crashes)
- Python 3.11+
- Network access to the NT8 share
- Access to Discord (webhooks and bot API)

**Discord:**
- A server you control (or have admin rights in)
- Three channels: status, fills, alerts
- A bot application registered at https://discord.com/developers/applications

---

## Setup

There are three pieces to wire up. Do them in this order — each one
verifies the previous one is working.

### Part 1 — NT8 side

1. Open NinjaTrader 8 → **New → NinjaScript Editor**.
2. In the left panel, right-click **Strategies** → **New** → cancel the wizard.
3. Right-click Strategies again → **New Strategy → File → New empty file**
   (exact wording varies by version; the goal is a blank `.cs` file in the
   Strategies folder).
4. Paste the contents of `MonitorExporter.cs` into the editor.
5. Press **F5** to compile. Bottom panel should say "Compile succeeded."
6. Create a folder for the JSON output: `C:\NT_Logger`. Make sure the
   Windows user running NT has write access to it.
7. Open any chart — a daily MES or MNQ chart works well as a host
   (low data activity, always has a reliable feed).
8. **Strategies tab** on that chart → add `MonitorExporter`. Configure:
   - **Output Folder**: `C:\NT_Logger`
   - **Poll Interval**: `30`
   - **Accounts**: `ALL` (or comma-separated list of specific accounts)
   - **Verbose Errors**: `True` for first run; flip to `False` once stable
9. Enable the strategy. Within 30 seconds you should see three files
   appear in `C:\NT_Logger\`:
   - `heartbeat.json`
   - `strategies.json`
   - `executions.log` (created on first fill — may not exist yet)
10. Open `heartbeat.json` in a text editor. Should contain a fresh
    `timestamp_utc` and a list of accounts.

#### Optional but recommended: auto-launch on boot

So the monitor comes back up after a Windows update or power blip:

1. Save your NT8 layout as a workspace (**Workspaces → Save As → Production**).
2. Edit the NT8 desktop shortcut → Properties → Target field. Append
   `-w="Production"` so it loads the workspace automatically:
   ```
   "C:\Program Files (x86)\NinjaTrader 8\bin\NinjaTrader.exe" -w="Production"
   ```
3. Use Task Scheduler instead of the Startup folder for reliability:
   - Trigger: **At log on of** [your user]
   - Delay: 1 minute (lets Windows finish booting)
   - Action: launch the modified shortcut
4. **Control Center → Tools → Options → Strategies** → enable the
   "Restore on startup" / "Re-enable on connection" options so strategies
   come back up automatically.
5. Reboot the NT machine once and verify `heartbeat.json` updates by itself
   without you doing anything.

### Part 2 — Python side

1. **Share the NT_Logger folder** from the NT8 machine, read-only:
   - Right-click `C:\NT_Logger` → Properties → Sharing → Advanced Sharing
   - Permissions: Read for a dedicated Windows user (recommend creating
     `nt_reader` rather than using your main account)
   - NTFS permissions on the Security tab: same — Read for that user

2. **Mount the share on the Python machine.**
   - Windows: `net use Z: \\NTMACHINE\NT_Logger /user:nt_reader /persistent:yes`
     (or map a drive in Explorer and check "Reconnect at sign-in")
   - Linux:
     ```
     sudo mount -t cifs //NTMACHINE/NT_Logger /mnt/nt_logger \
       -o username=nt_reader,password=...,ro,vers=3.0,uid=$(id -u)
     ```

3. **Clone the repo** on the Python machine:
   ```
   git clone https://github.com/YOUR_USER/YOUR_REPO.git
   cd YOUR_REPO
   ```

4. **Install Python dependencies:**
   ```
   pip install -r requirements.txt
   ```

5. **Configure account aliases.** Copy the example and edit:
   ```
   copy accounts.example.yaml accounts.yaml
   ```
   Open `accounts.yaml` and add an entry per account, mapping NT8 IDs
   (like `TDFYSL50351370795`) to friendly names with types
   (`funded`, `challenge`, `eval`, `sim`, `live`, `demo`).

6. **Test the reader before adding Discord:**
   ```
   python test_reader.py
   ```
   You should see:
   - `reachable: True` for both heartbeat and strategies
   - `stale: False` and `age: <30 seconds`
   - Your Tradeify/Apex/Sim accounts listed with friendly names
   - Strategies listed as enabled

   If `reachable: False`, the share isn't mounted or the path is wrong.
   If `stale: True` and the file's age is huge, the NT8 monitor isn't
   running.

### Part 3 — Discord side

1. **Create three channels** in your Discord server (or pick existing ones):
   - `#nt-status` — for the always-updated pinned status embed
   - `#nt-fills` — for fill notifications
   - `#nt-alerts` — for things going wrong

2. **Create a webhook for `#nt-fills`:**
   - Right-click the channel → Edit Channel → Integrations → Webhooks
   - **New Webhook**, name it ("NT Fills" or similar), copy the URL.
   - Repeat for `#nt-alerts`.
   - **Treat webhook URLs as secrets** — anyone with the URL can post.

3. **Create a Discord bot** (needed for editing the pinned status message
   in `#nt-status` — webhooks can post but can't edit messages older than
   ~14 minutes):
   - Go to https://discord.com/developers/applications → **New Application**
   - Give it a name like "NT Monitor"
   - **Bot tab** (left sidebar) → **Reset Token** → copy the token immediately
     (Discord only shows it once)
   - Privileged Gateway Intents: leave all OFF
   - **OAuth2 → URL Generator**:
     - Scopes: check **bot**
     - Bot permissions: check **Send Messages**, **Manage Messages**,
       **Read Message History**
     - Copy the generated URL at the bottom of the page
   - Paste that URL into a browser (while logged into Discord) → choose
     your server → Authorize. The bot now appears in your server's
     member list (offline until the service runs).

4. **Get the channel ID** of `#nt-status`:
   - In Discord: Settings → Advanced → enable Developer Mode
   - Right-click `#nt-status` → Copy Channel ID

5. **Configure the Python service.** Copy the example and edit:
   ```
   copy discord_config.example.yaml discord_config.yaml
   ```
   Fill in:
   - `bot.token` — the bot token from step 3
   - `bot.status_channel_id` — the channel ID from step 4
   - `bot.alert_mention_user_id` — your Discord user ID (right-click your
     own name → Copy User ID) if you want @mentions on errors, or `null`
   - `webhooks.fills` — webhook URL from step 2
   - `webhooks.alerts` — webhook URL from step 2

6. **Test the Discord pipeline before running the service for real:**
   ```
   python test_discord.py
   ```
   You should see three messages land in Discord:
   - Pinned status embed in `#nt-status`
   - Fake test fill in `#nt-fills`
   - Test alert + recovery in `#nt-alerts`

   If something fails, the script's stack trace will tell you what
   (usually wrong token, wrong channel ID, or bot not invited to the
   server). Fix and re-run.

---

## Running the service

```
python service.py
```

That's it. The service will:
- Bind the dashboard on `http://0.0.0.0:8080/` (reachable from anywhere on
  your local network)
- Edit the pinned status message in `#nt-status` every 30 seconds
- Post any new fills to `#nt-fills`
- Post alerts to `#nt-alerts` when things change
- Maintain `state.json` for per-symbol P&L

To access the dashboard from your phone (same WiFi):
1. Find the Python machine's local IP (`ipconfig` on Windows, `ip a` on Linux)
2. Open `http://192.168.x.x:8080/` in any phone browser
3. Browser menu → **Add to Home Screen** for an app-like icon

### Useful flags

```
python service.py --help
```

Common ones:
- `--once` — run a single poll and exit (good for testing)
- `--share Z:/` — override the share path (default `Z:/`)
- `--interval 30` — poll cadence in seconds
- `--no-web` — disable the dashboard
- `--web-host 127.0.0.1` — bind dashboard to localhost only (no LAN access)
- `--log-file nt_monitor.log` — write logs to a file as well as console
- `--log-level DEBUG` — verbose output for troubleshooting

### Running it persistently

For now, just leave it running in a PowerShell or terminal window. Long-term
options:
- **Windows:** wrap as a service with [NSSM](https://nssm.cc/) or run via
  Task Scheduler at logon
- **Linux:** systemd unit
- **VPS:** same as Linux above; just point `--share` at a remote-mounted
  CIFS or rsync target

---

## Files

| File | Purpose |
|---|---|
| `MonitorExporter.cs` | NinjaScript strategy. Compile inside NT8. |
| `service.py` | Main long-running Python service. |
| `nt_reader.py` | Reads heartbeat/strategies/executions from the share. |
| `discord_poster.py` | Discord webhook + bot API integration. |
| `pnl_tracker.py` | FIFO position tracking, per-symbol realized P&L. |
| `web_server.py` | Local HTTP server for the dashboard. |
| `accounts_config.py` | Loads `accounts.yaml`, applies aliases. |
| `accounts.yaml` | Your account aliases (gitignored). |
| `accounts.example.yaml` | Template for `accounts.yaml`. |
| `discord_config.yaml` | Bot token, webhooks, etc. (gitignored). |
| `discord_config.example.yaml` | Template for `discord_config.yaml`. |
| `state.json` | Persistent P&L state (gitignored, owned by the service). |
| `test_reader.py` | Smoke test for the share + reader. |
| `test_discord.py` | Smoke test for the full Discord pipeline. |

---

## Configuration reference

### `accounts.yaml`

Maps NT8 account IDs to friendly names and types. Hot-reloaded on change
— edit and save while the service is running, no restart needed.

```yaml
defaults:
  show_unlisted: false   # if true, accounts not in this file are shown with raw IDs

accounts:
  - id: TDFYSL50351370795
    alias: Tradify 50K #1
    type: funded         # one of: live, sim, funded, challenge, eval, demo
    notes: ""

  - id: Backtest
    hide: true           # explicitly suppress an internal NT account
```

### `discord_config.yaml`

```yaml
bot:
  token: "BOT_TOKEN"
  status_channel_id: 123456789012345678
  alert_mention_user_id: null     # or your Discord user ID for @mentions

webhooks:
  fills:  "https://discord.com/api/webhooks/..."
  alerts: "https://discord.com/api/webhooks/..."

behavior:
  status_edit_interval_sec: 30
  alert_dedupe_window_sec: 300    # don't repost the same alert within this window
  post_recovery: true             # post a green "recovered" message when alerts clear
```

### Contract multipliers

P&L computation uses futures contract multipliers defined in
`pnl_tracker.py`. NQ ($20/pt), MNQ ($2/pt), ES ($50/pt), and several
others are pre-populated. If you trade something not in the list, the
service will log a warning and use 1.0 as a fallback — fix by editing
`CONTRACT_MULTIPLIERS` at the top of `pnl_tracker.py`.

---

## Troubleshooting

**`reachable: False, error: heartbeat.json not found`** — share isn't
mounted, the path is wrong, or NT8 isn't running the monitor strategy.

**`reachable: True, stale: True, age: very large`** — NT process is up
but the monitor strategy is frozen, disabled, or the system clocks are
out of sync between the two machines. Run `w32tm /resync` on both Windows
machines and check that `heartbeat.json`'s timestamp updates.

**Discord 401 Unauthorized** — bot token is wrong, or the bot wasn't
invited to the server (the OAuth URL step).

**Discord 403 Forbidden on edit/pin** — bot is in the server but lacks
"Manage Messages" permission in `#nt-status`.

**Discord 404 on the channel** — channel ID is wrong (wrong server,
wrong channel, or channel was deleted).

**Webhook posts work, but pinned status message doesn't appear** — bot
posted but couldn't pin. Channel may already have 50 pins (Discord limit).
Unpin something old and the next poll will re-pin successfully.

**P&L numbers look wildly off** — almost always a missing contract
multiplier. Check the service logs for "No multiplier for instrument X"
warnings and add the symbol to `CONTRACT_MULTIPLIERS` in `pnl_tracker.py`.

**Strategy disabled alerts every time you stop trading** — by design.
The service can't tell "user pressed Disable" from "NT crashed and
disabled it." Eventually we may add quiet hours; for now, it's noise
you can ignore at end of day.

---

## Security notes

- **Never commit `discord_config.yaml`** — `.gitignore` should keep it
  out, but verify with `git check-ignore -v discord_config.yaml` before
  the first commit. If a token does leak into git history, **assume it
  is compromised**: reset the bot token in the Discord developer portal,
  delete and recreate any leaked webhooks, and update the local config
  file.
- **`state.json` contains no secrets** but does contain trade data —
  keep it gitignored anyway since it changes every poll and would create
  noisy diffs.
- **Dashboard has no authentication.** It's bound to `0.0.0.0` by default,
  which means anyone on your local network can see your account balances
  and positions. If you have untrusted devices on your WiFi, switch to
  `--web-host 127.0.0.1` (localhost only) or set up a firewall rule.
- **The SMB share for NT_Logger should be read-only.** The Python service
  doesn't need to write to it; restricting permissions limits blast radius
  if the Python machine is ever compromised.

---

## Roadmap / known gaps

- No support for currency contracts beyond a few common ones — add as needed.
- No daily P&L history (each session resets to zero). A persistent log
  would be a nice add.
- "Strategy disabled" alerts trigger when you intentionally stop
  strategies — would benefit from a quiet-hours config.
- The dashboard is read-only. No buttons to disable strategies or close
  positions remotely (and probably shouldn't be — that's a big risk
  surface for a personal tool).
- No support for the Tradovate API as a more authoritative data source.
  Looked into it but API access wasn't available on the user's tier.
- VPS deployment notes are sparse; documented as Linux + systemd in spirit
  but not battle-tested.
