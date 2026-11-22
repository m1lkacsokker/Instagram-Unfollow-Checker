# Instagram Unfollow Checker

> **AI Disclosure:** This project was designed and written with the assistance of AI (**Google Gemini**).

A lightweight, self-contained Python script that checks a target Instagram account's followers once per day using a secondary (burner) account, accurately detects who unfollowed, and sends push notifications via **[ntfy.sh](https://ntfy.sh)** and/or **Discord webhooks**.

Built with [`instagrapi`](https://github.com/subzeroid/instagrapi) following its [official best practices](https://subzeroid.github.io/instagrapi/latest/usage-guide/best-practices/) for mobile private API session persistence, device UUID stability, and human-like request pacing.

---

## Features

- **Persistent Device & Session Settings (`data/session.json`):** Avoids suspicious fresh password logins on every run by persisting cookies and Android device UUIDs (`load_settings` / atomic `dump_settings` with `0600` permissions), validating sessions with `get_timeline_feed()`, and preserving device UUIDs via `set_uuids()` on re-authentication.
- **ID-Based Diffing (No Rename False Positives):** Stores followers as `{"user_id": "username"}` in `data/followers_state.json`. If a follower changes their username, their handle is silently updated rather than reported as an unfollow.
- **Pagination-Drop Protection (Zero False Positives):**
  - Merges `user_followers_v1` with `user_followers_private_gql` whenever Instagram's ranked v1 pagination returns fewer users than the profile's follower count.
  - Verifies every candidate unfollower against Instagram's follower search index (`search_followers`) — including checking `user_info_v1` in case a user renamed *and* was omitted by pagination — before sending any unfollow alert.
  - Aborts safely if Instagram returns a severely truncated or empty response.
- **Desktop PC Rate-Limit & Sleep Safeguards:**
  - Enforces a **20-hour cooldown** via `data/last_run.txt` (bypassable with `--force`) so waking/rebooting your PC never spams Instagram.
  - Adds a **10–45 second randomized startup delay** and `delay_range = [1, 3]` between API calls.
- **Notifications & Health Alerts:** Sends unfollow alerts and high-priority failure alerts (`ChallengeRequired`, `TwoFactorRequired`, `FeedbackRequired`, `RateLimitError`, `LoginRequired`, etc.) to `NTFY_URL` and optional `DISCORD_WEBHOOK_URL`.
- **Rotating Logs:** Automatically logs runs, handle renames, errors, and unfollows to `data/checker.log` (2 MiB max, 5 rotated backups).

---

## Project Structure

```text
.
├── checker.py          # Main script
├── requirements.txt    # Python dependencies (instagrapi, requests, python-dotenv)
├── .env.example        # Environment variable template
├── .gitignore          # Excludes .venv/, .env, and data/
├── LICENSE             # MIT License
└── data/               # Created automatically at runtime (gitignored)
    ├── session.json         # Saved instagrapi session & device settings (0600)
    ├── followers_state.json # Baseline follower snapshot {user_id: username}
    ├── last_run.txt         # UTC ISO timestamp of last successful run
    └── checker.log          # Rotating log file
```

---

## Quick Start

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/m1lkacsokker/Instagram-Unfollow-Checker.git
cd Instagram-Unfollow-Checker

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

*(On Windows PowerShell, use `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, and `.venv\Scripts\python checker.py ...` for the commands below.)*

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
# Secondary (burner) Instagram account credentials
BOT_USERNAME=your_burner_username
BOT_PASSWORD=your_burner_password

# Target Instagram account to monitor (without @)
TARGET_USERNAME=target_account_username

# Push notification endpoint (e.g., https://ntfy.sh/your-private-topic-name)
NTFY_URL=https://ntfy.sh/your-secret-ig-topic

# Optional Discord webhook URL (leave blank to disable)
DISCORD_WEBHOOK_URL=

# Optional dedicated static proxy (stick to one stable IP/proxy per account)
PROXY_URL=
```

> **Tip:** Never use your main Instagram account as `BOT_USERNAME`. Use a secondary/burner account (ideally aged with normal activity and a profile photo). If the target account is private, make sure the burner account follows it.

### 3. Verify Setup (`--dry-run`)

Test your config, writable `data/` directory, diff logic, and push notifications without sending any requests to Instagram:

```bash
.venv/bin/python checker.py --dry-run
```

### 4. Initialize the Burner Session (`--init-session`)

Log in once interactively (prompts for 2FA or 6-digit email/SMS verification code if Instagram asks) and save `data/session.json`:

```bash
.venv/bin/python checker.py --init-session
```

### 5. Capture the Initial Followers Baseline

```bash
.venv/bin/python checker.py --force
```

Subsequent runs (manual or scheduled) will compare against this baseline and notify you when someone unfollows.

---

## CLI Flags

| Flag | Description |
| :--- | :--- |
| `--init-session` | Log into `BOT_USERNAME` interactively and persist device/session settings to `data/session.json`. |
| `--force` | Bypass the 20-hour cooldown check in `data/last_run.txt`. |
| `--dry-run` | Verify `.env`, paths, `instagrapi` client setup, diff self-test, and notification delivery without contacting Instagram. |

---

## Daily Native OS Autorun (With Missed-Run Catch-Up)

### Linux (`systemd` User Timer)

Ensure the user systemd directory exists (`mkdir -p ~/.config/systemd/user`), then create `~/.config/systemd/user/ig-unfollow-checker.service` (replace `/path/to/Instagram-Unfollow-Checker` with your actual directory):

```ini
[Unit]
Description=Instagram Unfollow Checker (daily follower diff and notification)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/Instagram-Unfollow-Checker
ExecStart=/path/to/Instagram-Unfollow-Checker/.venv/bin/python /path/to/Instagram-Unfollow-Checker/checker.py
Nice=10
```

Create `~/.config/systemd/user/ig-unfollow-checker.timer`:

```ini
[Unit]
Description=Run Instagram Unfollow Checker once daily with missed-run catch-up

[Timer]
OnCalendar=daily
Persistent=true
Unit=ig-unfollow-checker.service

[Install]
WantedBy=timers.target
```

Enable and start the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now ig-unfollow-checker.timer
systemctl --user list-timers ig-unfollow-checker.timer --all
```

Because `Persistent=true` is enabled, if your computer was turned off or asleep at midnight, `systemd` automatically runs the missed check the next time you boot or wake up (and `checker.py`'s 20-hour cooldown prevents duplicate runs).

### Windows (Task Scheduler via PowerShell)

Run in PowerShell (replace `C:\path\to\Instagram-Unfollow-Checker` with your actual directory):

```powershell
$Repo = "C:\path\to\Instagram-Unfollow-Checker"
$Action = New-ScheduledTaskAction `
    -Execute "$Repo\.venv\Scripts\pythonw.exe" `
    -Argument "$Repo\checker.py" `
    -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -Daily -At 12:00PM
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName "IG-Unfollow-Checker" `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Daily Instagram unfollow checker with missed-run catch-up"
```

Using `pythonw.exe` runs silently in the background with no console window popup, and `-StartWhenAvailable` catches up missed runs after boot or wake.

---

## Troubleshooting

- **`ChallengeRequired` / Checkpoint alert:** Open Instagram in your browser or phone app logged into the burner account, complete any verification prompt, and then re-run `.venv/bin/python checker.py --init-session`.
- **`FeedbackRequired` / `PleaseWaitFewMinutes`:** Instagram temporarily soft-blocked automated requests on the burner account or IP. Stop running `--force`, disable any VPN, dismiss any warning in the Instagram app, and wait 24–48 hours.
- **Follower count slightly lower than Instagram's displayed number:** Instagram's profile counter often includes deactivated or flagged-for-review accounts that Instagram does not return in follower list APIs.

---

## Disclaimer

This project is provided for personal and educational use. Automated access to Instagram may violate Instagram's Terms of Service and can result in rate limits, verification challenges, or account suspension. Use a dedicated burner account and run conservatively (once per day).

## License

[MIT](LICENSE)
