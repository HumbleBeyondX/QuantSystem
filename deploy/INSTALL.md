# QuantSystem — Installation & First Run

## Install

1. Double-click `QuantSystem Setup <version>.exe` (from `desktop/dist/`).
2. Windows SmartScreen will flag the installer as unsigned on first run —
   click **More info** → **Run anyway**. (Code signing cert TBD.)
3. Accept the NSIS wizard defaults, or choose a custom install directory.
4. Desktop & Start-Menu shortcuts are created automatically.

## First Run

On first launch, QuantSystem does three things automatically:

1. **Scaffolds the app data directory** at `%APPDATA%\QuantSystem\`:
   ```
   %APPDATA%\QuantSystem\
     .env                          (empty template — fill this in)
     config\
       deployments.yaml            (VPS registry — edit to add new servers)
       reports.yaml                (daily report schedule & thresholds)
       live\                       (live trading config defaults)
       strategies\                 (strategy parameter YAMLs)
     data\                         (positions, SQLite DB, cache)
     logs\                         (backend + electron log files)
   ```

2. **Shows a setup dialog** if `.env` is missing required OKX API keys.
   Click **Open .env** to edit the file in your default text editor. The
   required variables are:
   - `OKX_API_KEY`
   - `OKX_API_SECRET`
   - `OKX_PASSPHRASE`

   (Without these, crypto trading features are disabled. The app will
   still monitor your Tokyo VPS and send daily reports.)

3. **Starts the bundled backend** (`qs-backend.exe`) on `127.0.0.1:8002`,
   waits for its `/health` endpoint, then loads the UI.

## Day-to-Day Use

- **Launch**: double-click the desktop icon. A single instance at a time —
  launching a second time just brings the existing window to focus.
- **Tray menu**: right-click the system-tray icon for quick access to:
  - Show window
  - Emergency stop (sends to the backend's live runner, if any)
  - Open app data folder / Edit .env / Open logs folder
  - Quit
- **Closing the window** hides it to the tray. Use tray → Quit to exit fully.

## Where Things Live

| Purpose                | Location                                            |
|------------------------|-----------------------------------------------------|
| Installed app          | `C:\Users\<you>\AppData\Local\Programs\QuantSystem\` |
| User data (persistent) | `%APPDATA%\QuantSystem\`                            |
| Backend log            | `%APPDATA%\QuantSystem\logs\backend.log`            |
| Electron log           | `%APPDATA%\QuantSystem\logs\electron.log`           |
| SQLite DB              | `%APPDATA%\QuantSystem\data\quantsystem.db`         |

## Troubleshooting

**The app window doesn't open / "Backend failed to start"**
- Check `%APPDATA%\QuantSystem\logs\electron.log` for Electron errors
- Check `%APPDATA%\QuantSystem\logs\backend.log` for Python errors
- In the UI, go to the **Logs** page to view/export a support bundle
- Port conflict? The backend needs `127.0.0.1:8002` — if occupied, close
  the other process (check Task Manager for stale `qs-backend.exe`)

**Backend keeps crashing**
- The watchdog auto-restarts up to 3 times with 2s / 5s / 15s backoff.
- After 3 failures, you'll see a crash dialog with a link to the logs folder.
- Look for `crash-<timestamp>.log` in the logs folder — contains the reason
  the watchdog gave up.

**Email reports not sending**
- Verify Gmail OAuth2 is set up — see
  `dashboard/alerts/email_notifier.py --setup` (must be run once from
  the source checkout, NOT from the installed app).
- OAuth tokens live in `%APPDATA%\QuantSystem\data\gmail_token.json`.
- Alternative: set `ALERT_EMAIL_SENDER` + `ALERT_EMAIL_PASSWORD` (app
  password) in `.env` and the fallback SMTP path will kick in.

## Uninstall

Use **Settings → Apps → QuantSystem → Uninstall** (or the Start-Menu
uninstaller).

By default the uninstaller preserves `%APPDATA%\QuantSystem\` (your
configs, logs, and database). To wipe everything, delete that folder
manually after uninstalling.

## Rebuilding from Source

```powershell
# From the project root:
.\deploy\build_installer.ps1
```

This runs the full pipeline (frontend → backend bundle → smoke test →
installer). Output: `desktop\dist\QuantSystem Setup <version>.exe`.
