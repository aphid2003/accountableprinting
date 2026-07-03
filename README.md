# Print accountability app

## What's new (this update)
- **Admin section is now blue** (employee-facing pages stay green/brass).
- **Auto-updates for all installed copies** — see `UPDATES.md` for how to
  ship a new version to everyone via GitHub Releases; users get a banner
  with an "Update now" button, no manual reinstall.
- **Print settings** — employees can now set copies, single/double-sided,
  color vs. black & white, orientation, and paper size before printing
  (sent to the printer via PJL commands; works on PJL-aware printers like
  Kyocera). Settings are shown per job on the admin dashboard.
- **Optional central monitoring via MongoDB Atlas** — see `central_api/README.md`.
  Each desktop app can report print events to a small central API you host,
  which writes to Atlas, so you can watch prints across all 100+ machines
  from one place. This is off by default (`central_config.json`), and
  printing always works locally even if this is disabled or unreachable.

---

Employees log every print job (name, department, client, purpose, page count)
right before printing — no approval, no waiting. They submit, then print
immediately. The value is the paper trail: admins get a live, filterable log
of who printed what and why, totals by employee and department, a CSV export,
and the ability to block anyone who's misusing the system going forward.

## What it does NOT do
This does not gate or authorize printing. It does not talk to your Kyocera
printer directly. It's a lightweight accountability record that sits
alongside normal printing — think of it as the digital version of the paper
logbook, just automatically searchable and reportable.

---

## 1. Setup (local test run)

You need Python 3 installed. Then:

```bash
cd print_app
pip install flask --break-system-packages
python3 app.py
```

Visit:
- **Employee form:** http://localhost:5000
- **Admin dashboard:** http://localhost:5000/admin/login

**Default admin login:** username `admin`, password `changeme123`

⚠️ Before using this for real, open `app.py` and change these two lines near
the top:
```python
app.secret_key = "CHANGE-THIS-SECRET-KEY-IN-PRODUCTION"
ADMIN_PASSWORD_HASH = generate_password_hash("changeme123")
```
Pick a long random secret key and a strong admin password.

---

## 2. How it works day-to-day

**Employees:**
1. Go to the app link (bookmark it, or put a QR code sticker on the printer)
2. Enter employee ID, name, department, client/project, purpose, page count
3. Click "Log and continue to print" — done, no waiting
4. Print as normal

**Admins:**
1. Log in at `/admin/login`
2. See the full activity log, filterable by department and time window (7/30/90 days/all time)
3. See running totals by department and by employee — this is where patterns jump out (e.g. one person way above everyone else)
4. Export the full log as CSV any time for deeper analysis or to email to finance/management
5. Block any employee from logging further jobs — they'll see a clear message if they try

---

## 3. Running this for real at the company (not just your laptop)

Right now `python3 app.py` runs Flask's built-in development server — fine for
testing, not for production. For real company use:

### Option A — simplest (small office, one server/PC always on)
1. Install a production WSGI server:
   ```bash
   pip install waitress --break-system-packages
   ```
2. Replace the last two lines of `app.py`:
   ```python
   if __name__ == "__main__":
       init_db()
       from waitress import serve
       serve(app, host="0.0.0.0", port=5000)
   ```
3. Run it on a machine that stays on (a small always-on PC or a cheap VM),
   and make sure other computers on the network can reach it — everyone
   visits `http://<that-machine's-ip>:5000`.

### Option B — proper server deployment (recommended for a real company)
1. Get a small Linux server or VM (even a $5-10/month cloud VM works fine
   for this scale).
2. Install `gunicorn` as the WSGI server:
   ```bash
   pip install gunicorn --break-system-packages
   gunicorn -w 2 -b 0.0.0.0:8000 app:app
   ```
3. Put `nginx` in front of it as a reverse proxy, and enable HTTPS with a
   free certificate (Let's Encrypt via `certbot`). This step matters — admin
   login credentials and employee names shouldn't travel over plain HTTP on
   a real network.
4. Run gunicorn as a `systemd` service so it restarts automatically if the
   server reboots. Example `/etc/systemd/system/printapp.service`:
   ```ini
   [Unit]
   Description=Print accountability app
   After=network.target

   [Service]
   User=www-data
   WorkingDirectory=/opt/print_app
   ExecStart=/usr/bin/gunicorn -w 2 -b 127.0.0.1:8000 app:app
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```bash
   sudo systemctl enable printapp
   sudo systemctl start printapp
   ```

### Database note
This uses SQLite (`print_app.db`), which is perfectly fine for a single
office with normal usage. If you grow past ~50-100 concurrent users or want
multiple servers, swap to PostgreSQL — ask me and I'll adapt the code.

---

## 4. Getting people to actually use it

- Put a QR code linking straight to the employee form on/near the printer —
  removes the "where do I even go" friction
- Consider requiring the employee ID field to match your real staff list,
  so names can't be typo'd or faked (ask me if you want an "only known
  employee IDs allowed" version)
- Review the department totals monthly — this is usually where the useful
  conversations start, not from chasing individual logs

## 5. What I can build next, if useful
- Scheduled email of the weekly/monthly CSV report straight to your inbox
  (reusing the email script pattern from earlier)
- Cross-check declared page counts against the printer's actual Job
  Accounting counts, and flag mismatches automatically
- Restrict submissions to only pre-registered employee IDs
