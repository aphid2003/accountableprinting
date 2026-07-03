# Accountable Printing — Application Report
### Architecture, Production Readiness, and Maintenance

**Version covered:** 1.0.0 · **Prepared:** July 2026

---

## 1. Executive Summary

Accountable Printing is a Windows desktop application that logs every print
job an employee sends (who, department, client, purpose, pages), routes the
job to a network printer, and gives admins a searchable record with totals,
exports, and employee management. It is packaged as a native installer and
currently runs on a per-machine basis with a local database.

This update round took it from a single-machine tool toward a fleet-manageable
product for 100+ installs:

| Capability | Status |
|---|---|
| Self-updating across all installs | ✅ Built (GitHub Releases based) |
| Fleet-wide print monitoring | ✅ Built (optional, central API + MongoDB Atlas) |
| Per-job print settings (copies, duplex, color, etc.) | ✅ Built |
| Admin visual identity matching brand | ✅ Built |
| Production-grade secrets & auth | ⚠️ Needs action before rollout |
| Backups / data durability | ⚠️ Needs a decision |
| Support/monitoring tooling | ⚠️ Not built (recommended, not required) |

The app is **functionally ready to pilot**. It is **not yet ready for
unattended fleet rollout** until the items in Section 4 ("Must fix before
production") are addressed — none of them are large, but they're the
difference between "works on my machine" and "safe to hand to 100 employees."

---

## 2. What the app does

**Employees:**
1. Open the app (desktop shortcut) → fill in employee ID, name, department,
   client/project, purpose, page count, attach file(s).
2. Choose a printer, adjust print settings (copies, single/double-sided,
   color/B&W, orientation, paper size), optionally mark it private.
3. Print. Private jobs are held server-side until released with a PIN at
   the printer station ("My Jobs").

**Admins:**
1. Log in at `/admin/login`.
2. See a live, filterable, sortable log of every job, with per-job print
   settings, totals by department and employee, CSV export, and a monthly
   PDF report.
3. Manage employees (add, ban/unban, regenerate PIN), departments, and
   known printer IP/port mappings.
4. See an update banner when a newer version has been published, with a
   one-click "Update now."

---

## 3. Architecture

### 3.1 Component overview

```
┌───────────────────────────── Employee's Windows PC ─────────────────────────────┐
│                                                                                    │
│   pywebview window  ──renders──▶  Flask app (127.0.0.1:5000, background thread)   │
│                                        │                                          │
│                                        ├─ SQLite (print_app.db)  ← source of      │
│                                        │      truth for THIS machine              │
│                                        │                                          │
│                                        ├─ Printer comms:                          │
│                                        │    1. Windows Spooler RAW (win32print)   │
│                                        │    2. Raw TCP socket to port 9100        │
│                                        │    3. OS print dialog (last resort)      │
│                                        │    → all paths inject a PJL header       │
│                                        │      (account ID, copies, duplex, color, │
│                                        │       orientation, paper size)           │
│                                        │                                          │
│                                        ├─ GitHub Releases API (update check)      │
│                                        │      → downloads + silently runs new     │
│                                        │        installer when available          │
│                                        │                                          │
│                                        └─ (optional) POST print event ───────────┼──▶ Central API
└────────────────────────────────────────────────────────────────────────────────┘         │
                                                                                              ▼
                                                                                   ┌─────────────────────┐
                                                                                   │  Central API server  │
                                                                                   │  (Flask, you deploy) │
                                                                                   │  auth: API key       │
                                                                                   └──────────┬───────────┘
                                                                                              │
                                                                                              ▼
                                                                                   ┌─────────────────────┐
                                                                                   │   MongoDB Atlas      │
                                                                                   │   print_events coll. │
                                                                                   └─────────────────────┘
```

### 3.2 Packaging & distribution

- **Runtime:** PyInstaller `--onedir` build (`build.bat`) → a folder with
  `print_app.exe` and all dependencies.
- **Installer:** Inno Setup (`setup.iss`) wraps that folder into
  `AccountablePrinting_Installer.exe`, with a fixed `AppId` so future
  versions upgrade in place rather than duplicating the install.
- **Per-machine data:** stored outside the install folder, in
  `%APPDATA%\AccountablePrintingApp\` — survives reinstalls/updates:
  - `print_app.db` (SQLite)
  - `uploads\` (temp storage for files mid-print; deleted after printing)
  - `central_config.json` (central monitoring on/off + endpoint + key)
  - Log file: `%USERPROFILE%\print_app.log`

### 3.3 Auto-update mechanism

- On each app launch (once per session, not per page), the app calls
  `GET api.github.com/repos/<owner>/repo>/releases/latest`.
- If the tag is newer than the running `APP_VERSION`, a banner appears with
  "Update now."
- Clicking it downloads the release's `.exe` asset to a temp folder and runs
  it with `/VERYSILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`, which closes
  the running app, overwrites files, and reopens it — no admin needs to
  touch the machine.
- Full operational steps live in `UPDATES.md` in the project.
- **Failure mode by design:** if GitHub is unreachable, the check fails
  silently and the app behaves exactly as before — updating is additive,
  never blocking.

### 3.4 Print settings & printer communication

Jobs are sent as **raw PJL + PDF bytes**, bypassing the Windows print driver
entirely (this is why account codes and private/hold printing work). Print
settings (copies, duplex, color, orientation, paper size) are now injected
as standard PJL `SET` commands into that same header. This is why it's
fast and driver-independent, but also why:
- It depends on the printer understanding PJL (true for Kyocera, most HP
  LaserJets, and most enterprise mono/color laser printers; **not** for
  cheap inkjets or driver-only USB printers).
- Color/duplex keyword support varies slightly by printer vendor — the
  current keyword set (`RENDERMODE`, `DUPLEX`, `BINDING`, `PAPER`,
  `ORIENTATION`) is the widely-supported baseline; a given fleet may need
  one or two keywords tuned per printer model (see Section 6).

### 3.5 Central monitoring (optional, off by default)

- A **separate** small Flask service (`central_api/`) is the only thing
  that holds the MongoDB Atlas connection string — no desktop machine ever
  sees it.
- Desktop app → central API: authenticated with a shared or per-device
  `X-API-Key`, fire-and-forget (a failed report never blocks or delays
  printing; the local SQLite log remains authoritative for that machine
  either way).
- Central API → your dashboard/BI tool: authenticated with a separate
  `X-Admin-Key`. Endpoints exist for raw event listing and a
  department-summary aggregation; MongoDB Atlas's own Charts feature can
  also point at the collection directly with no extra code.
- **This is currently disabled by default** (`central_config.json:
  enabled=false`) until you've deployed the central API and Atlas cluster —
  see `central_api/README.md`.

### 3.6 Visual identity

- Admin login/dashboard use a navy (`#002858`) + gold (`#F0781E`) theme
  sampled directly from the company logo; employee-facing pages keep the
  original green/brass theme. Scoped via a single `body.admin-theme` CSS
  class so the two never bleed into each other.
- Favicon and app/taskbar icon were regenerated from a tight crop of the
  logo's mark (not the full wordmark), fixing legibility at 16–32px.

---

## 4. Production-readiness assessment

### 4.1 Must fix before rolling out to real users

| # | Item | Why it matters | Effort |
|---|---|---|---|
| 1 | **Change `ADMIN_PASSWORD_HASH`** (currently hashes the literal string `"changeme123"`) | Anyone who reads the source or guesses the default gets admin access to every install | 2 min |
| 2 | **Change `app.secret_key`** (currently the literal placeholder) | Flask session cookies are signed with this; a shared, public key lets someone forge an admin session | 2 min |
| 3 | **Set `UPDATE_REPO`** to your real GitHub repo | Update checks currently point at a placeholder and will silently no-op | 2 min |
| 4 | **Decide the admin password rollout plan** — one shared password across 100 admins/managers, or per-person accounts? | Current design is a single username/password, fine for 1-3 admins, awkward at scale | 1-2 hrs if moving to per-person |

None of these require new architecture — 1-3 are literal one-line edits.

### 4.2 Should fix soon (not blocking a pilot)

| Item | Why | Notes |
|---|---|---|
| No CSRF protection on admin forms | Standard hardening for any session-authenticated form (ban employee, delete log, etc.) | Add `flask-wtf` CSRF tokens — small, contained change |
| No login rate-limiting | Admin login has no lockout/backoff, so it's brute-forceable if exposed beyond localhost | Only matters if `/admin/login` is ever reachable over a network, not just `127.0.0.1` — confirm this stays local-only, or add rate limiting if not |
| No automated backup of `print_app.db` | It's the only copy of that machine's print history; a disk failure loses it | See 5.3 |
| `central_api` `DEVICE_API_KEYS` defaults to "allow all" if unset | Safe for local testing, **not** for a real deployment | Documented in `central_api/README.md`; just remember to set it before deploying |
| Atlas Network Access set to `0.0.0.0/0` during setup | Fine to get started, should be narrowed to your central API host's IP once known | One settings change in Atlas |

### 4.3 Already solid

- **Parameterized SQL throughout** — no injection risk found in a pass over all 46 `db.execute()` call sites.
- **Printing has three fallback layers** (Windows Spooler RAW → raw TCP socket → OS print dialog), so a single failure mode doesn't stop employees from printing.
- **Uploaded files are deleted immediately after a successful print**, and queued (private) files only persist until released — data isn't accumulating indefinitely on disk.
- **Central monitoring fails safe** — printing works identically whether or not the central API is reachable.
- **Database schema migrations use the `ALTER TABLE ... / except OperationalError: pass` pattern** consistently, so shipping an update never crashes an existing install with an older schema.

---

## 5. Deployment guide (summary)

Full detail lives in `README.md`, `UPDATES.md`, and `central_api/README.md`.
This is the condensed sequence.

### 5.1 First rollout
1. Fix the four "must fix" items in 4.1.
2. `build.bat` → produces `dist\AccountablePrinting_Installer.exe`.
3. Distribute that installer to the first batch of machines (manually, via
   a shared drive, or your imaging/deployment tool — whichever you already
   use for installing other line-of-business apps).
4. Confirm each machine can see the printers on its subnet and successfully
   send a test print.

### 5.2 Every future update
1. Bump `APP_VERSION` (`app.py`) and `AppVersion` (`setup.iss`) — must match.
2. `build.bat`.
3. Publish a GitHub Release tagged `v<version>` with the installer attached.
4. Every installed copy sees the update banner next time it's opened.
   No manual redistribution needed.

### 5.3 Data durability (recommendation, not yet built)
Each machine's `print_app.db` is local SQLite — good for reliability
(no network dependency to log a print) but each machine's history lives
only on that machine. Two low-effort options, pick based on whether you've
enabled central monitoring:
- **If central monitoring is on:** MongoDB Atlas already has your full
  fleet-wide print history: Atlas's own automated backups cover you.
  Local SQLite becomes a "recent local cache," not your only copy.
- **If central monitoring is off:** consider a scheduled task that copies
  `%APPDATA%\AccountablePrintingApp\print_app.db` to a shared network
  drive nightly. A few lines of PowerShell + Windows Task Scheduler; ask
  if you want this written.

---

## 6. Maintenance runbook

**Routine (as needed):**
- *Ship a bug fix or feature* → follow 5.2.
- *Add/remove an admin* → currently single shared admin login; see 4.1 item 4 if you want per-person accounts instead.
- *Employee joins/leaves* → Admin dashboard → Employees (add, or ban rather than delete, to preserve their historical log entries).
- *Printer IP changes* → Admin dashboard → Printer Settings.
- *A printer doesn't respect a print setting* (e.g. ignores duplex) → that model likely uses a slightly different PJL keyword; check its PJL/PCL reference manual and adjust `_build_pjl_header()` in `app.py` — isolated, low-risk change.

**Periodic (recommend a calendar reminder):**
- *Quarterly:* rotate `app.secret_key` requires all users to re-login next launch — low-impact, worth doing occasionally.
- *Quarterly:* review Atlas Network Access list and API keys in `central_api/.env` if central monitoring is enabled.
- *Monthly, if central monitoring is on:* check MongoDB Atlas usage/cost — the free M0 tier has storage limits that a 100-person fleet will eventually exceed.

**Troubleshooting:**
- App-level errors: `%USERPROFILE%\print_app.log` on the affected machine (plain text, timestamped, append-only).
- Central API errors: wherever you deployed it (Render/Railway dashboard logs, or `journalctl` if self-hosted via systemd).
- "Update now" doesn't appear: confirm `UPDATE_REPO` is set correctly and the machine has outbound access to `api.github.com`.

---

## 7. Known limitations

- Print-setting keywords (color/duplex especially) are best-effort across
  printer brands — verified conceptually against the PJL standard, not
  tested against your specific fleet's printer models.
- Admin authentication is a single shared username/password, not
  per-person accounts with audit trail of *who* banned an employee or
  deleted a log, for example.
- No automated test suite exists yet; changes were manually smoke-tested
  (route responses, template rendering, theme scoping) but there's no
  regression safety net for future changes.
- Central API has no built-in dashboard UI yet — it exposes data via REST
  endpoints and is designed to be paired with Atlas Charts or a BI tool,
  not a bundled admin screen.

---

## 8. Recommended next steps, in order

1. Complete the four items in 4.1 (15 minutes total).
2. Pilot on 5-10 machines for a week; watch `print_app.log` on those machines.
3. Decide central monitoring now or later — it's fully optional and safe to enable after the fact.
4. Roll out to the remaining machines.
5. Revisit Section 4.2 items once the pilot is stable — they're hardening, not blockers.

---

## Appendix A — Project file map

```
print_app/
├── app.py                  Main application (Flask + pywebview + print logic)
├── build.bat                PyInstaller + Inno Setup build script
├── setup.iss                 Inno Setup installer definition
├── icon.ico                  App/taskbar/installer icon
├── README.md                  Setup, features, deployment options
├── UPDATES.md                  How to ship a new version to the fleet
├── templates/                  Jinja2 HTML templates (employee + admin UI)
├── static/img/                  Logo, favicon (multi-size)
├── central_api/
│   ├── server.py                 Central API (Flask + pymongo)
│   ├── requirements.txt
│   ├── .env.example
│   └── README.md                  Atlas + deployment walkthrough
└── (runtime, not in repo) %APPDATA%\AccountablePrintingApp\
    ├── print_app.db                 SQLite — this machine's log
    ├── uploads\                       Transient, deleted after printing
    └── central_config.json            Central monitoring toggle + credentials
```

## Appendix B — Key configuration values to set before go-live

| File | Variable | Current value | Action |
|---|---|---|---|
| `app.py` | `ADMIN_PASSWORD_HASH` | hash of `"changeme123"` | Set a real password |
| `app.py` | `app.secret_key` | placeholder string | Set a long random value |
| `app.py` | `UPDATE_REPO` | `"YOUR-GITHUB-USERNAME/print_app"` | Set to your real repo |
| `setup.iss` | `AppVersion` | `1.0.0` | Keep in sync with `APP_VERSION` on every release |
| `central_api/.env` | `MONGODB_URI` | unset | Your Atlas connection string, if using central monitoring |
| `central_api/.env` | `DEVICE_API_KEYS` / `ADMIN_API_KEY` | unset | Long random strings, if using central monitoring |
