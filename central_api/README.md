# Central Print Monitoring API

This is a separate small service. The desktop app never connects to MongoDB
directly — it POSTs a print event here, and this service writes it to
MongoDB Atlas. It also serves the admin monitoring dashboard's data.

## 1. Set up MongoDB Atlas (free tier is fine to start)

1. Go to https://www.mongodb.com/cloud/atlas/register and create an account.
2. Create a free (M0) cluster.
3. Under **Database Access**, create a database user with a strong password.
4. Under **Network Access**, add the IP address of wherever you'll host this
   API (or `0.0.0.0/0` to start, then lock it down once you know your host's
   IP — 0.0.0.0/0 is fine for testing, not for long-term production).
5. Click **Connect → Drivers**, copy the connection string. It looks like:
   `mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority`

## 2. Configure this service

```bash
cd central_api
cp .env.example .env
```

Edit `.env`:
- `MONGODB_URI` — the connection string from step 1 (fill in your real user/password)
- `DEVICE_API_KEYS` — a long random string (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`).
  Every desktop app will send this in the `X-API-Key` header.
- `ADMIN_API_KEY` — a different long random string, used only by your monitoring dashboard.

## 3. Run it locally to test

```bash
pip install -r requirements.txt
python server.py
```

Test it:
```bash
curl http://localhost:8000/healthz
```

## 4. Deploy it somewhere always-on

Any of these work — pick based on what you're comfortable with:

**Render.com (free tier, easiest)**
1. Push this `central_api` folder to a GitHub repo (can be the same repo as
   the desktop app, or its own).
2. On Render: New → Web Service → connect the repo → set:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn -w 2 -b 0.0.0.0:$PORT server:app`
3. Add the same environment variables from your `.env` in Render's dashboard.
4. Render gives you a URL like `https://your-service.onrender.com` — that's
   your `api_url` for the desktop app config (step 5).

**Railway.app** — same idea, similarly simple, has a free trial then paid.

**Your own VPS** — run with gunicorn behind nginx + HTTPS (same pattern as
described in the main README.md's "Option B" for the whole app), as a
systemd service.

## 5. Point the desktop app at this API

On each employee machine, this app writes a small config file at:
- Windows: `%APPDATA%\AccountablePrintingApp\central_config.json`

Edit it (or push it out via your deployment/imaging process) to:
```json
{
  "enabled": true,
  "api_url": "https://your-service.onrender.com",
  "api_key": "the-same-string-as-DEVICE_API_KEYS-in-.env"
}
```

Once `enabled` is `true` and the URL/key are set, every completed print job
is also reported here in the background. If this API is unreachable, printing
still works normally — the event is just silently dropped, and each
machine's local SQLite log (visible in that machine's own `/admin`) remains
the full record for that device either way.

## 6. Reading the data (your monitoring dashboard)

- `GET /api/v1/print-events?limit=200` with header `X-Admin-Key: <ADMIN_API_KEY>`
  — most recent print events across every device.
- `GET /api/v1/print-events/summary` (same header) — totals by department.

You can point a simple internal web page, a BI tool, or MongoDB Atlas's own
Charts feature (built into Atlas, no extra code) directly at the
`print_events` collection for graphs/dashboards without writing any more code.
