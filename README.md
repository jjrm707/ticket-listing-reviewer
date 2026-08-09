# Ticket Listing Reviewer

This guide is for the Windows-local version-one reviewer. Run commands from the repository root in PowerShell.

## 1. What the reviewer does and does not do.

The reviewer is personal, local, read-only decision support for pairs of tickets to Houston Texans home games at NRG Stadium (the former Reliant Stadium name is recognized) and Texas A&M football home games at Kyle Field. It ranks estimates using a default `$400` all-in pair cap, a `$50` estimated-net alert threshold, and a `$20` improvement before a repeat alert. It scans immediately at startup and hourly while the PC is on.

It does not buy, reserve, list, relist, sell, reprice, transfer, or scrape tickets. It never automates a consumer marketplace page, makes no transaction, and offers no profit guarantee. The user checks every listing and makes every transaction manually.

## 2. Python environment.

Install official CPython 3.12 or 3.13 for Windows. An incompatible MSYS2/MINGW Python can cause native-package or Windows-path failures; use the official `python.org` build or Windows `py` launcher. Quote paths containing spaces.

```powershell
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Activation is optional. You can use the repository interpreter explicitly:

```powershell
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe --version
```

If `python` resolves to MSYS2/MINGW, close that shell and use `py -3.12`, `py -3.13`, or `.venv\Scripts\python.exe`.

## 3. Copy `.env.example` to `.env`.

```powershell
Copy-Item -LiteralPath .\.env.example -Destination .\.env
```

`.env` is local and untracked. Keep the loopback, hourly, and `TR_DRY_RUN=true` defaults. Never commit `.env`, paste secrets into logs/issues, or include credentials, screenshots, topics, or tokens in screenshots.

## 4. Official API registration and capability caveats.

These official guide links were verified on 2026-08-09:

- Ticketmaster: [developer portal](https://developer.ticketmaster.com/) and [Discovery getting started](https://developer.ticketmaster.com/products-and-docs/apis/getting-started/). This app uses authorized public Discovery event data only.
- SeatGeek: [developer registration](https://seatgeek.com/build) and [API documentation](https://seatgeek.github.io/). Its public event prices, counts, and popularity are comparison signals.
- StubHub: [developer portal](https://developer.stubhub.com/) and [OAuth/API overview](https://developer.stubhub.com/docs/overview/introduction/). This app uses application-only OAuth with Catalog/read:events access exposed to the issued credentials.

Ticketmaster Discovery, SeatGeek event aggregates, and the current StubHub Catalog integration are event-level signals. They cannot alone confirm a buyable adjacent pair or trigger a pair alert. TickPick has no automated connector: a TickPick URL is an inert private reference for a user-supplied screenshot and is never fetched. Credentials, free access, scopes, fields, and approval are not guaranteed and may change; follow current provider terms. Do not scrape consumer pages.

## 5. Install Tesseract.

Follow the official [Tesseract installation documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html). Its Windows section links to current third-party Windows installers. Add the executable directory to `PATH`, include English language data, restart PowerShell, then verify:

```powershell
tesseract --version
```

OCR is optional and local. The correction form is authoritative; never confirm OCR text without checking it.

## 6. Set up free ntfy on iPhone.

Follow the [ntfy phone subscription guide](https://docs.ntfy.sh/subscribe/phone/) and [getting-started documentation](https://docs.ntfy.sh/). Install the iOS app. Generate a random high-entropy topic of 24–128 characters locally, subscribe to that exact topic, and place it only in `TR_NTFY_TOPIC` in `.env`. Public ntfy topics are bearer-like and unguessable rather than private merely by name; anyone who learns the topic can address it unless access controls are configured. `TR_NTFY_ACCESS_TOKEN` is optional.

Generate a 64-character topic locally in PowerShell, confirm its length, then copy the displayed value only into `.env` and the iPhone subscription:

```powershell
$topicBytes = New-Object byte[] 32
$topicRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$topicRng.GetBytes($topicBytes)
$topicRng.Dispose()
$ntfyTopic = -join ($topicBytes | ForEach-Object { $_.ToString('x2') })
$ntfyTopic.Length
$ntfyTopic
```

Use the Settings notification test before normal alerts. Never publish the topic/token in screenshots, source control, logs, issues, or chat.

## 7. Run migrations.

Back up an existing database first. Then run the migration and confirm the exact head:

```powershell
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m alembic current
```

Expected current revision: `0001_initial (head)`.

## 8. Start locally.

```powershell
& .\scripts\run.ps1
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Useful pages are `/healthz`, `/`, `/manual`, `/settings`, and `/health`. The launcher is hard-coded to `127.0.0.1:8765`; Host validation rejects LAN/public addressing, and an OS lock beside the database allows one application process. Stop cleanly with `Ctrl+C` so the scheduler, clients, database engine, and lock are released.

## 9. Verify dry-run with real visible data.

Keep `TR_DRY_RUN=true`. Available official APIs can automatically collect event-level signals, but without authorized listing detail an actionable pair generally requires a screenshot you supply and correct. Manually compare at least three current public signals without asking the app to fetch marketplace pages. Check asking-price inputs, public links, timestamp/freshness, fees, estimated tax, seller-fee assumption, confidence, and risks. Upload one of your own PNG/JPEG screenshots, correct every OCR suggestion, confirm quantity two and all-in cost, and inspect the deterministic score.

Dry-run qualifying decisions and fingerprints are stored with provider `dry-run`, but no normal push is delivered. Event floors and aggregates never become confirmed pairs.

## 10. Enable normal alerts only after test.

In Settings, explicitly submit the notification test and verify its receipt on the iPhone. Restart and confirm history remains. Only then stop the app, manually set `TR_DRY_RUN=false` in `.env`, and restart.

Normal pushes still require a confirmed, fresh, medium/high-confidence pair at or below the budget with at least `$50` estimated net profit. A repeat needs a `$20` improvement. This does not enable auto-purchase. The pre-send reservation is safe for the normal one-process deployment; it is not a distributed exactly-once guarantee and never creates a false sent record after a failed publish.

## 11. Install/uninstall Windows startup.

The scripts use a non-admin current-user logon task named exactly `TicketListingReviewer`, a hidden PowerShell window, `StartWhenAvailable`, and Task Scheduler `MultipleInstances IgnoreNew` as another one-instance layer.

```powershell
& .\scripts\install_startup_task.ps1
Get-ScheduledTask -TaskName TicketListingReviewer -TaskPath '\'
```

Sign out and back in, then confirm one task and one local listener:

```powershell
Get-ScheduledTask -TaskName TicketListingReviewer -TaskPath '\'
Get-NetTCPConnection -State Listen -LocalAddress 127.0.0.1 -LocalPort 8765
```

Safe uninstall removes only the exact task:

```powershell
& .\scripts\uninstall_startup_task.ps1
Get-ScheduledTask -TaskPath '\' | Where-Object TaskName -ne 'TicketListingReviewer'
```

Inspect unrelated tasks before and after. Automated tests parse these semantics but do not install a real task or sign the user out.

## 12. Backup/delete local history and screenshots.

Stop the app first. The narrow local data paths are `data\ticket_reviewer.db` (plus SQLite sidecars while running) and `data\screenshots\`. Choose an explicit backup directory, copy exact paths, and inspect it:

```powershell
$reviewerBackup = 'C:\Users\you\Documents\TicketReviewerBackup-2026-08-09'
New-Item -ItemType Directory -Path $reviewerBackup
Copy-Item -LiteralPath .\data\ticket_reviewer.db -Destination $reviewerBackup
Copy-Item -LiteralPath .\data\screenshots -Destination $reviewerBackup -Recurse
Get-ChildItem -LiteralPath $reviewerBackup
```

After verifying the backup and confirming the app is stopped, delete only an explicitly inspected file:

```powershell
Get-Item -LiteralPath .\data\ticket_reviewer.db
Remove-Item -LiteralPath .\data\ticket_reviewer.db
Get-ChildItem -LiteralPath .\data\screenshots
# Replace this filename with one exact file shown above:
Remove-Item -LiteralPath .\data\screenshots\0123456789abcdef0123456789abcdef.png
```

Never use a broad recursive deletion command. Screenshots can also be deleted individually in the UI. Deleting the DB removes history, outcomes, settings, and alert fingerprints and may allow future repeat notifications.

## 13. Estimates and responsibility.

Estimates use visible asking prices, not completed sales. Buyer fees, tax, and seller-fee rates may be assumptions. Event signals may be too granular to identify seats, inventory changes, provider rules/access change, and resale/regulatory constraints vary. Recorded outcomes are local history, not a trained model. Results are not guaranteed. The user checks current laws, marketplace terms, source freshness, listing identity, and every transaction manually.

## First-run checklist

- [ ] Install official Python 3.12/3.13 and dependencies.
- [ ] Copy `.env.example` to local `.env`; leave dry-run enabled.
- [ ] Add only available official credentials and optional local Tesseract/ntfy settings.
- [ ] Back up any existing DB, migrate to `0001_initial (head)`, and start with `scripts\run.ps1`.
- [ ] Open `127.0.0.1`, inspect health, compare three signals, and confirm one screenshot score.
- [ ] Submit the explicit ntfy test and verify the phone before disabling dry-run.

## Troubleshooting and health status

- `Repository-local Python executable is unavailable`: create `.venv` with official Windows Python.
- Migration/database error: stop duplicates, confirm the local SQLite parent is writable, and run `alembic current`.
- `application is already running`: use the existing instance or stop it cleanly.
- Missing/degraded source in `/health`: verify required credential pairs and current access. Event-only health can be successful while automatic actionable pairs remain unavailable.
- OCR unavailable: verify `tesseract --version`, restart PowerShell after changing `PATH`, or correct fields manually.
- No push: keep dry-run on while checking the exact topic/subscription, then use only the explicit Settings test. Errors intentionally omit secrets and raw provider bodies.

## Test commands

```powershell
& .\.venv\Scripts\python.exe -m pytest -v
& .\.venv\Scripts\python.exe -m alembic upgrade head
& .\.venv\Scripts\python.exe -m alembic current
git diff --check
```

## Manual acceptance checklist

### Verified automatically here

- [x] Empty-DB migration and exact schema/head.
- [x] Synthetic fixture scans/dashboard, `$60.50` dry-run estimate, zero delivery, and event-signal no-alert behavior.
- [x] PNG upload, injected OCR suggestion, correction/confirmation, deterministic scoring, and inert reference URL.
- [x] Replay/restart persistence of observations, opportunity history, outcome lineage, and alert fingerprints.
- [x] Localhost Host rejection, startup-script semantics, single-process guard, exact task name, and uninstall scope.

### User-run after configuring local secrets/tools

- [ ] Start with `TR_DRY_RUN=true` and open `http://127.0.0.1:8765`.
- [ ] Confirm the machine's actual LAN address is unreachable.
- [ ] Configure only available keys and inspect each health status.
- [ ] Compare at least three public signals manually without automated page fetching.
- [ ] Upload and correct the user's own screenshot.
- [ ] Submit the explicit ntfy test and verify receipt on the iPhone.
- [ ] Restart and inspect persisted history/fingerprints.
- [ ] Install the task, sign out/in, confirm one process, uninstall it, and inspect unrelated tasks.

These user-only steps have not been executed by the automated test run.
