"""
Print Accountability App
-------------------------
Employees log every print job (name, department, client, purpose, pages) right
before printing. There's no approval gate — the log happens instantly and the
employee proceeds to print immediately. The point is the paper trail: admin can
see who printed what, when, and why, spot unusual patterns, and block repeat
offenders from logging further jobs if needed.

Run locally with:
    pip install flask --break-system-packages
    python3 app.py

Then visit http://localhost:5000 (employee form)
and http://localhost:5000/admin/login (admin dashboard)

Default admin login: username "admin", password "changeme123"
CHANGE THIS before using in production (see ADMIN_PASSWORD below).
"""

import sqlite3
import csv
import io
import os
import uuid
import socket
import platform
import subprocess
import random
import PyPDF2
import threading
import time
import tempfile
import json as _json
import urllib.request
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
import webview
import webbrowser
from functools import wraps
from datetime import datetime, date
from flask import (
    Flask, request, render_template, redirect, url_for,
    session, flash, g, Response, send_from_directory, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = "CHANGE-THIS-SECRET-KEY-IN-PRODUCTION"  # required for sessions

# ---- Hide console windows on Windows ----
# When packaged with PyInstaller's --windowed flag, the main app has no console.
# But every subprocess.check_output/run/Popen call (powershell, lpstat, etc.)
# would otherwise briefly flash its own black CMD window. This helper adds the
# flags needed to keep those windows hidden, and is merged into every
# subprocess call below via **_NO_WINDOW.
if platform.system() == "Windows":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _STARTUPINFO.wShowWindow = subprocess.SW_HIDE
    _NO_WINDOW = {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": _STARTUPINFO,
    }
else:
    _NO_WINDOW = {}

# Use a persistent user directory for data so it isn't lost when running as an .exe
if platform.system() == 'Windows':
    app_data_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'AccountablePrintingApp')
else:
    app_data_dir = os.path.join(os.path.expanduser('~'), '.accountable_printing_app')

os.makedirs(app_data_dir, exist_ok=True)

UPLOAD_FOLDER = os.path.join(app_data_dir, 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

DATABASE = os.path.join(app_data_dir, "print_app.db")

# ---- Change this before real use ----
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = generate_password_hash("changeme123")
# --------------------------------------

# ---- Auto-update config ----
# Bump APP_VERSION on every release. UPDATE_REPO is your GitHub "owner/repo".
# Publish new versions as GitHub Releases with the built installer .exe
# attached as a release asset, tagged e.g. "v1.1.0". See UPDATES.md.
APP_VERSION = "1.0.1"
UPDATE_REPO = "aphid2003/accountableprinting"
# -----------------------------


# ---------- Central monitoring (optional MongoDB Atlas via central API) ----------
# The desktop app never talks to MongoDB directly. It POSTs a small JSON event
# to a central API server (see /central_api in this project) after each print,
# which is the thing that actually writes to MongoDB Atlas. This keeps the
# Atlas connection string off 100+ machines and lets you revoke a device's
# access centrally. Config lives in a small JSON file so it can be rolled out
# without rebuilding the app.

CENTRAL_CONFIG_PATH = os.path.join(app_data_dir, "central_config.json")


def _load_central_config():
    default = {"enabled": False, "api_url": "", "api_key": "", "device_id": None}
    try:
        if os.path.exists(CENTRAL_CONFIG_PATH):
            with open(CENTRAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                default.update(_json.load(f))
    except Exception as e:
        log_message("WARNING", f"Could not read central_config.json: {e}")

    if not default.get("device_id"):
        default["device_id"] = str(uuid.uuid4())
        try:
            with open(CENTRAL_CONFIG_PATH, "w", encoding="utf-8") as f:
                _json.dump(default, f, indent=2)
        except Exception:
            pass
    return default


def report_print_event(log_row_dict, print_settings=None):
    """Fire-and-forget POST of a print event to the central API. Never raises,
    never blocks printing — if the central server or internet is unreachable,
    the event is simply dropped (the local SQLite log is always the source
    of truth for this machine)."""
    cfg = _load_central_config()
    if not cfg.get("enabled") or not cfg.get("api_url"):
        return

    def _send():
        try:
            payload = {
                "device_id": cfg["device_id"],
                "employee_code": log_row_dict.get("employee_code"),
                "name": log_row_dict.get("name"),
                "department": log_row_dict.get("department"),
                "client": log_row_dict.get("client"),
                "purpose": log_row_dict.get("purpose"),
                "pages": log_row_dict.get("pages"),
                "printer": log_row_dict.get("queued_printer") or log_row_dict.get("printer"),
                "print_settings": print_settings or {},
                "printed_at": datetime.now().isoformat(),
            }
            body = _json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                cfg["api_url"].rstrip("/") + "/api/v1/print-events",
                data=body,
                headers={"Content-Type": "application/json", "X-API-Key": cfg.get("api_key", "")},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=8)
        except Exception as e:
            log_message("WARNING", f"Central monitoring report failed (non-fatal): {e}")

    threading.Thread(target=_send, daemon=True).start()


# ---------- Database helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DATABASE)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            banned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS print_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_code TEXT NOT NULL,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            log_date TEXT NOT NULL,
            pages INTEGER NOT NULL,
            client TEXT NOT NULL,
            purpose TEXT NOT NULL,
            created_at TEXT NOT NULL,
            file_name TEXT,
            private_print INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'printed',
            queued_printer TEXT
        );

        CREATE TABLE IF NOT EXISTS departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        );

        CREATE TABLE IF NOT EXISTS printer_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            printer_name TEXT UNIQUE NOT NULL,
            printer_ip TEXT NOT NULL,
            printer_port INTEGER NOT NULL DEFAULT 9100
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS update_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_version TEXT,
            to_version TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    # Migrate: add printer_settings table if it doesn't exist
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS printer_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                printer_name TEXT UNIQUE NOT NULL,
                printer_ip TEXT NOT NULL,
                printer_port INTEGER NOT NULL DEFAULT 9100
            )
        """)
    except sqlite3.OperationalError:
        pass

    # Migrate: add private_print column to print_logs if it doesn't exist
    try:
        conn.execute("ALTER TABLE print_logs ADD COLUMN private_print INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migrate: add status column (queued / printed)
    try:
        conn.execute("ALTER TABLE print_logs ADD COLUMN status TEXT NOT NULL DEFAULT 'printed'")
    except sqlite3.OperationalError:
        pass

    # Migrate: add queued_printer column
    try:
        conn.execute("ALTER TABLE print_logs ADD COLUMN queued_printer TEXT")
    except sqlite3.OperationalError:
        pass

    # Migrate: add print-settings columns (copies / duplex / color / orientation / paper size)
    for col, ddl in [
        ("copies", "INTEGER NOT NULL DEFAULT 1"),
        ("duplex", "TEXT NOT NULL DEFAULT 'simplex'"),      # simplex | duplex_long | duplex_short
        ("color_mode", "TEXT NOT NULL DEFAULT 'color'"),     # color | grayscale
        ("orientation", "TEXT NOT NULL DEFAULT 'portrait'"), # portrait | landscape
        ("paper_size", "TEXT NOT NULL DEFAULT 'a4'"),        # a4 | letter | legal
    ]:
        try:
            conn.execute(f"ALTER TABLE print_logs ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def record_app_version():
    """Called once at startup. Compares the running APP_VERSION against the
    last version this machine recorded. If it changed, logs a row in
    update_history so the admin dashboard can show that an update actually
    took effect (and when) — not just that one was offered."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = 'last_version'"
        ).fetchone()
        last_version = row["value"] if row else None

        if last_version is None:
            # First run ever on this machine — nothing to compare against,
            # just record the baseline, no history entry.
            conn.execute(
                "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('last_version', ?)",
                (APP_VERSION,)
            )
        elif last_version != APP_VERSION:
            conn.execute(
                "INSERT INTO update_history (from_version, to_version, updated_at) VALUES (?, ?, ?)",
                (last_version, APP_VERSION, datetime.now().isoformat())
            )
            conn.execute(
                "UPDATE app_meta SET value = ? WHERE key = 'last_version'",
                (APP_VERSION,)
            )
        conn.commit()
    finally:
        conn.close()


def get_employee(employee_code):
    db = get_db()
    return db.execute(
        "SELECT * FROM employees WHERE employee_code = ?", (employee_code,)
    ).fetchone()

# ---------- Auth helpers ----------

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


# ---------- Employee-facing routes ----------

@app.route("/", methods=["GET", "POST"])
def employee_form():
    if request.method == "POST":
        employee_code = request.form.get("employee_code", "").strip()
        client = request.form.get("client", "").strip()
        purpose = request.form.get("purpose", "").strip()
        documents = request.files.getlist("document")

        if not all([employee_code, client, purpose]) or not documents or documents[0].filename == '':
            flash("Please fill in every field and upload at least one document.", "error")
            return redirect(url_for("employee_form"))

        saved_files = []
        total_pages = 0
        
        for doc in documents:
            filename = secure_filename(doc.filename)
            if filename:
                unique_filename = f"{uuid.uuid4().hex}_{filename}"
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                doc.save(file_path)
                saved_files.append(unique_filename)
                
                # Securely count pages on the backend
                try:
                    with open(file_path, 'rb') as f:
                        pdf = PyPDF2.PdfReader(f)
                        total_pages += len(pdf.pages)
                except Exception:
                    total_pages += 1
        
        if total_pages == 0: total_pages = 1
        combined_filenames = ",".join(saved_files)

        db = get_db()
        employee = get_employee(employee_code)
        
        if not employee:
            flash("Invalid 4-digit PIN.", "error")
            return redirect(url_for("employee_form"))

        if employee["banned"]:
            flash(
                "You're currently blocked from logging print jobs. "
                "Contact your administrator.",
                "error",
            )
            return redirect(url_for("employee_form"))
            
        name = employee["name"]
        department = employee["department"]
        private_print = 1 if request.form.get("private_print") else 0

        cursor = db.execute(
            """INSERT INTO print_logs
               (employee_code, name, department, log_date, pages, client, purpose, created_at, file_name, private_print)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                employee_code, name, department, date.today().isoformat(),
                total_pages, client, purpose, datetime.now().isoformat(), combined_filenames,
                private_print
            ),
        )
        db.commit()
        log_id = cursor.lastrowid
        
        return redirect(url_for("select_printer", log_id=log_id))

    return render_template("employee.html")


# ---------- Printing helpers & routes ----------

def log_message(level, message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] [{level}] {message}"
    print(log_line)
    try:
        # Save logs to the user's home directory so they are always writable
        log_path = os.path.join(os.path.expanduser("~"), "print_app.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as e:
        print(f"Failed to write to log file: {e}")

def clean_name(name):
    return "".join(c for c in name.lower() if c.isalnum())

def get_system_printers():
    printers = []
    if platform.system() == "Windows":
        try:
            output = subprocess.check_output(
                ['powershell', '-Command', 'Get-Printer | Select-Object Name | ConvertTo-Json'],
                **_NO_WINDOW
            ).decode()
            import json
            data = json.loads(output)
            if isinstance(data, list):
                printers = [p['Name'] for p in data]
            elif isinstance(data, dict):
                printers = [data['Name']]
        except Exception as e:
            log_message("ERROR", f"Failed to list Windows printers: {e}")
    else:
        try:
            output = subprocess.check_output(['lpstat', '-p'], **_NO_WINDOW).decode()
            for line in output.splitlines():
                if line.startswith('printer '):
                    parts = line.split(' ')
                    if len(parts) > 1:
                        printers.append(parts[1])
        except Exception as e:
            log_message("ERROR", f"Failed to list Linux printers: {e}")
    return printers

def get_printer_device_uri(printer_name):
    """Return socket://host:port URI for the given printer, checking DB config first."""
    # Check admin-configured printer IPs first (works on all platforms)
    try:
        db = get_db()
        rows = db.execute("SELECT printer_name, printer_ip, printer_port FROM printer_settings").fetchall()
        target_cleaned = clean_name(printer_name)
        log_message("INFO", f"Matching printer '{printer_name}' (cleaned: '{target_cleaned}') against DB...")
        for row in rows:
            cfg_name = row['printer_name']
            cfg_cleaned = clean_name(cfg_name)
            log_message("INFO", f"Comparing with DB printer config: '{cfg_name}' (cleaned: '{cfg_cleaned}')")
            if cfg_cleaned == target_cleaned or cfg_cleaned in target_cleaned or target_cleaned in cfg_cleaned:
                log_message("INFO", f"Match found in DB! Using configured IP: {row['printer_ip']}:{row['printer_port']}")
                return f"socket://{row['printer_ip']}:{row['printer_port']}"
    except Exception as e:
        log_message("ERROR", f"Error checking printer settings from DB: {e}")

    # Fallback to local system printer discovery
    if platform.system() == "Windows":
        try:
            import json
            cmd = f'Get-Printer -Name "{printer_name}" | Select-Object PortName | ConvertTo-Json'
            output = subprocess.check_output(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd],
                timeout=6, stderr=subprocess.DEVNULL, **_NO_WINDOW
            ).decode()
            data = json.loads(output)
            port_name = data.get('PortName', '') if isinstance(data, dict) else ''
            log_message("INFO", f"Windows fallback: port name for '{printer_name}' is '{port_name}'")
            if port_name.startswith("IP_"):
                return f"socket://{port_name[3:]}:9100"
            elif port_name.count('.') == 3:
                return f"socket://{port_name}:9100"
        except Exception as e:
            log_message("ERROR", f"Windows fallback discovery failed: {e}")
    else:
        try:
            output = subprocess.check_output(
                ['lpstat', '-v', printer_name], stderr=subprocess.DEVNULL, **_NO_WINDOW
            ).decode().strip()
            log_message("INFO", f"Linux fallback: lpstat output: '{output}'")
            if ": " in output:
                return output.split(": ", 1)[1].strip()
        except Exception as e:
            log_message("ERROR", f"Linux fallback discovery failed: {e}")
    return None

def _resolve_windows_printer_ip(printer_name):
    """Multiple PowerShell attempts to get the network IP of a Windows printer."""
    log_message("INFO", f"Attempting advanced Windows IP resolution for '{printer_name}'...")
    methods = [
        # Method 1: Get-PrinterPort PrinterHostAddress
        f'$p=(Get-Printer -Name "{printer_name}").PortName; (Get-PrinterPort -Name $p).PrinterHostAddress',
        # Method 2: WMI PortName (may already be an IP)
        f'(Get-WmiObject Win32_Printer | Where-Object {{$_.Name -eq "{printer_name}"}}).PortName',
    ]
    for i, cmd in enumerate(methods, 1):
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                timeout=6, stderr=subprocess.DEVNULL, **_NO_WINDOW
            ).decode().strip()
            log_message("INFO", f"Method {i} output: '{out}'")
            parts = out.split(".")
            if len(parts) == 4 and all(p.isdigit() for p in parts):
                return out
            if out.startswith("IP_"):
                return out[3:]
        except Exception as e:
            log_message("WARNING", f"Method {i} failed: {e}")
            continue
    return None

PJL_PAPER_NAMES = {"a4": "A4", "letter": "LETTER", "legal": "LEGAL"}


def _build_pjl_header(account_id, is_private=False, print_settings=None):
    """Build the PJL header bytes, optionally with private/hold print commands
    and job-level print settings (copies, duplex, color, orientation, paper size).

    print_settings is a dict with keys: copies, duplex, color_mode, orientation, paper_size.
    These are standard HP PJL keywords, which Kyocera and most PJL-aware laser
    printers understand. Some models use slightly different keywords for color
    mode — check your printer's PJL reference if grayscale/color doesn't apply.
    """
    print_settings = print_settings or {}
    acc_bytes = account_id.encode('ascii') if account_id else b""
    uel = b"\x1b%-12345X"
    header = (
        uel +
        b"@PJL JOB NAME=\"AccountablePrint\"\r\n"
        b"@PJL SET JOBATTR=\"ACNT=" + acc_bytes + b"\"\r\n"
    )

    copies = int(print_settings.get("copies") or 1)
    if copies > 1:
        header += f"@PJL SET COPIES={copies}\r\n".encode('ascii')
        header += f"@PJL SET QTY={copies}\r\n".encode('ascii')

    duplex = print_settings.get("duplex", "simplex")
    if duplex == "duplex_long":
        header += b"@PJL SET DUPLEX=ON\r\n@PJL SET BINDING=LONGEDGE\r\n"
    elif duplex == "duplex_short":
        header += b"@PJL SET DUPLEX=ON\r\n@PJL SET BINDING=SHORTEDGE\r\n"
    else:
        header += b"@PJL SET DUPLEX=OFF\r\n"

    color_mode = print_settings.get("color_mode", "color")
    if color_mode == "grayscale":
        header += b"@PJL SET RENDERMODE=GRAYSCALE\r\n@PJL SET COLOR=OFF\r\n"
    else:
        header += b"@PJL SET COLOR=ON\r\n"

    orientation = print_settings.get("orientation", "portrait")
    header += f"@PJL SET ORIENTATION={'LANDSCAPE' if orientation == 'landscape' else 'PORTRAIT'}\r\n".encode('ascii')

    paper_size = PJL_PAPER_NAMES.get(print_settings.get("paper_size", "a4"), "A4")
    header += f"@PJL SET PAPER={paper_size}\r\n".encode('ascii')

    if is_private:
        log_message("INFO", "Private print mode: injecting HOLD/HOLDTYPE/HOLDKEY into PJL header.")
        header += (
            b"@PJL SET HOLD=ON\r\n"
            b"@PJL SET HOLDTYPE=PRIVATE\r\n"
            b"@PJL SET HOLDKEY=\"" + acc_bytes + b"\"\r\n"
        )
    header += b"@PJL ENTER LANGUAGE=PDF\r\n"
    return header

def raw_socket_print(filepaths, host, port, printer_name, account_id, is_private=False, print_settings=None):
    """Send PDF files to printer via raw TCP socket with PJL account code header."""
    import socket as _socket
    uel = b"\x1b%-12345X"
    pjl_header = _build_pjl_header(account_id, is_private, print_settings)
    pjl_footer = uel + b"@PJL EOJ\r\n" + uel

    for fp in filepaths:
        log_message("INFO", f"Preparing file for raw socket send: {fp}")
        with open(fp, 'rb') as f:
            pdf_data = f.read()
        payload = pjl_header + pdf_data + pjl_footer
        
        log_message("INFO", f"Connecting socket to {host}:{port}...")
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect((host, port))
        log_message("INFO", f"Sending {len(payload)} bytes of raw payload...")
        s.sendall(payload)
        s.close()
        log_message("INFO", f"Socket connection closed successfully.")

def win32_spool_print(filepaths, printer_name, account_id, is_private=False, print_settings=None):
    """Print PDF files by sending raw PJL + PDF data through the Windows Print Spooler."""
    try:
        import win32print
    except ImportError:
        log_message("WARNING", "win32print module not found. Skipping spooler raw print.")
        return False

    uel = b"\x1b%-12345X"
    pjl_header = _build_pjl_header(account_id, is_private, print_settings)
    pjl_footer = uel + b"@PJL EOJ\r\n" + uel

    log_message("INFO", f"Attempting Windows Spooler print for '{printer_name}' (private={is_private})...")
    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        try:
            for fp in filepaths:
                log_message("INFO", f"Reading file for Windows Spooler: {fp}")
                with open(fp, 'rb') as f:
                    pdf_data = f.read()
                payload = pjl_header + pdf_data + pjl_footer
                
                log_message("INFO", f"Sending raw print job through Windows Spooler...")
                hJob = win32print.StartDocPrinter(hPrinter, 1, ("AccountablePrintJob", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hPrinter)
                    win32print.WritePrinter(hPrinter, payload)
                    win32print.EndPagePrinter(hPrinter)
                    log_message("INFO", f"Successfully spooled {len(payload)} bytes to '{printer_name}'.")
                except Exception as spool_err:
                    log_message("ERROR", f"Spooler page print failed: {spool_err}")
                    raise spool_err
                finally:
                    win32print.EndDocPrinter(hPrinter)
            return True
        finally:
            win32print.ClosePrinter(hPrinter)
    except Exception as e:
        log_message("ERROR", f"Windows Spooler RAW printing failed: {e}")
        return False

def execute_print(filepaths, printer_name, page_range=None, account_id=None, is_private=False, print_settings=None):
    """Main print entry point. Always tries raw TCP socket first."""
    log_message("INFO", f"--- Starting execute_print for '{printer_name}' (Account ID: {account_id}) ---")

    # Step 1: On Windows, try spooling RAW data directly through the Windows Spooler first
    log_message("INFO", f"Private print mode: {is_private} | Settings: {print_settings}")
    if platform.system() == "Windows":
        success = win32_spool_print(filepaths, printer_name, account_id, is_private, print_settings)
        if success:
            log_message("INFO", "Windows Spooler RAW print succeeded!")
            return

    # Step 2: resolve the printer IP via CUPS (Linux) or PowerShell (Windows)
    uri = get_printer_device_uri(printer_name)
    log_message("INFO", f"get_printer_device_uri returned: '{uri}'")

    # Step 3: Windows fallback — try multiple PowerShell methods
    if not uri and platform.system() == "Windows":
        ip = _resolve_windows_printer_ip(printer_name)
        if ip:
            uri = f"socket://{ip}:9100"
            log_message("INFO", f"Windows IP resolved via PowerShell: {ip}")

    # Step 4: Send via raw TCP socket (works on Linux + Windows)
    if uri and uri.startswith("socket://"):
        try:
            host_port = uri[9:]
            if "/" in host_port:
                host_port = host_port.split("/")[0]
            host, port_str = (host_port.rsplit(":", 1) if ":" in host_port else (host_port, "9100"))
            port = int(port_str)
            log_message("INFO", f"Attempting raw socket connection to {host}:{port}")
            raw_socket_print(filepaths, host, port, printer_name, account_id, is_private, print_settings)
            log_message("INFO", "Raw socket print succeeded!")
            return
        except Exception as e:
            log_message("ERROR", f"Raw socket print failed: {e}")

    # Step 5: Last-resort fallback — no account ID support
    log_message("WARNING", "Could not print via raw socket or spooler. Falling back to system print dialog/spooler.")
    if platform.system() == "Windows":
        def _do_print():
            for fp in filepaths:
                try:
                    log_message("INFO", f"Calling os.startfile for print on: {fp}")
                    os.startfile(fp, "print")
                except Exception as ex:
                    log_message("ERROR", f"startfile error: {ex}")
        threading.Thread(target=_do_print, daemon=True).start()
    else:
        cmd = ['lp', '-d', printer_name]
        if page_range:
            cmd.extend(['-o', f'page-ranges={page_range}'])
        cmd.extend(filepaths)
        log_message("INFO", f"Running fallback LP command: {' '.join(cmd)}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        log_message("INFO", f"LP stdout: {res.stdout.strip()} | stderr: {res.stderr.strip()}")


@app.route("/select_printer/<int:log_id>", methods=["GET", "POST"])
def select_printer(log_id):
    db = get_db()
    log_entry = db.execute("SELECT * FROM print_logs WHERE id = ?", (log_id,)).fetchone()
    if not log_entry:
        flash("Print log not found.", "error")
        return redirect(url_for("employee_form"))

    if request.method == "POST":
        printer_name = request.form.get("printer")
        page_range = request.form.get("page_range", "").strip()
        account_id = log_entry["employee_code"]
        if not printer_name:
            flash("Please select a printer.", "error")
            return redirect(url_for("select_printer", log_id=log_id))

        # Collect print settings from the form (with safe defaults/validation)
        try:
            copies = max(1, min(99, int(request.form.get("copies", "1") or 1)))
        except ValueError:
            copies = 1
        duplex = request.form.get("duplex", "simplex")
        if duplex not in ("simplex", "duplex_long", "duplex_short"):
            duplex = "simplex"
        color_mode = request.form.get("color_mode", "color")
        if color_mode not in ("color", "grayscale"):
            color_mode = "color"
        orientation = request.form.get("orientation", "portrait")
        if orientation not in ("portrait", "landscape"):
            orientation = "portrait"
        paper_size = request.form.get("paper_size", "a4")
        if paper_size not in ("a4", "letter", "legal"):
            paper_size = "a4"

        print_settings = {
            "copies": copies,
            "duplex": duplex,
            "color_mode": color_mode,
            "orientation": orientation,
            "paper_size": paper_size,
        }
        db.execute(
            "UPDATE print_logs SET copies=?, duplex=?, color_mode=?, orientation=?, paper_size=? WHERE id=?",
            (copies, duplex, color_mode, orientation, paper_size, log_id)
        )
        db.commit()

        # Update logged pages if a specific range is used
        if page_range:
            try:
                count = 0
                for part in page_range.split(','):
                    part = part.strip()
                    if '-' in part:
                        s, e = part.split('-')
                        count += (int(e) - int(s) + 1)
                    else:
                        int(part)
                        count += 1
                if count > 0:
                    # Multiply by number of files since CUPS applies the range to each file
                    actual_pages = count * len([f for f in log_entry["file_name"].split(',') if f])
                    db.execute("UPDATE print_logs SET pages = ? WHERE id = ?", (actual_pages, log_id))
                    db.commit()
            except ValueError:
                flash("Invalid page range format.", "error")
                return redirect(url_for("select_printer", log_id=log_id))
        
        saved_filenames = log_entry["file_name"].split(',')
        file_paths = [os.path.join(app.config['UPLOAD_FOLDER'], fn) for fn in saved_filenames if fn]
        
        valid_paths = [fp for fp in file_paths if os.path.exists(fp)]
        
        if valid_paths:
            is_private = bool(log_entry["private_print"])
            if is_private:
                # Queue the job on the server — do NOT send to printer yet
                db.execute(
                    "UPDATE print_logs SET status='queued', queued_printer=? WHERE id=?",
                    (printer_name, log_id)
                )
                db.commit()
                flash(
                    "Your job has been queued. Go to 'My Jobs' at the printer station "
                    "and enter your PIN to release and print it.",
                    "success"
                )
                return redirect(url_for("my_jobs"))
            else:
                execute_print(valid_paths, printer_name, page_range, account_id, is_private=False, print_settings=print_settings)
                report_print_event({**dict(log_entry), "queued_printer": printer_name}, print_settings)
                # Delete files immediately after printing
                for fp in valid_paths:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                return redirect(url_for("print_success", log_id=log_id))
        else:
            flash("File not found on server.", "error")
            return redirect(url_for("employee_form"))

    printers = get_system_printers()
    return render_template("select_printer.html", printers=printers, log_id=log_id, log_entry=log_entry)


@app.route("/my_jobs", methods=["GET", "POST"])
def my_jobs():
    """Job queue page — employee enters PIN to see and release their held jobs."""
    jobs = []
    pin = ""
    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        employee = get_employee(pin)
        if not employee:
            flash("Invalid PIN. Please try again.", "error")
        else:
            db = get_db()
            jobs = db.execute(
                """SELECT * FROM print_logs
                   WHERE employee_code = ? AND status = 'queued'
                   ORDER BY created_at DESC""",
                (pin,)
            ).fetchall()
            if not jobs:
                flash(f"No queued jobs found for {employee['name']}.", "success")
    return render_template("my_jobs.html", jobs=jobs, pin=pin)


@app.route("/release_job/<int:log_id>", methods=["POST"])
def release_job(log_id):
    """Release a queued private print job — send it to the printer now."""
    db = get_db()
    log_entry = db.execute("SELECT * FROM print_logs WHERE id = ?", (log_id,)).fetchone()
    if not log_entry:
        flash("Job not found.", "error")
        return redirect(url_for("my_jobs"))

    if log_entry["status"] != "queued":
        flash("This job has already been printed or removed.", "error")
        return redirect(url_for("my_jobs"))

    printer_name = log_entry["queued_printer"]
    account_id   = log_entry["employee_code"]

    saved_filenames = (log_entry["file_name"] or "").split(",")
    file_paths = [os.path.join(app.config["UPLOAD_FOLDER"], fn) for fn in saved_filenames if fn]
    valid_paths = [fp for fp in file_paths if os.path.exists(fp)]

    if not valid_paths:
        flash("Files for this job are no longer available on the server.", "error")
        db.execute("UPDATE print_logs SET status='printed' WHERE id=?", (log_id,))
        db.commit()
        return redirect(url_for("my_jobs"))

    try:
        queued_settings = {
            "copies": log_entry["copies"] if "copies" in log_entry.keys() else 1,
            "duplex": log_entry["duplex"] if "duplex" in log_entry.keys() else "simplex",
            "color_mode": log_entry["color_mode"] if "color_mode" in log_entry.keys() else "color",
            "orientation": log_entry["orientation"] if "orientation" in log_entry.keys() else "portrait",
            "paper_size": log_entry["paper_size"] if "paper_size" in log_entry.keys() else "a4",
        }
        execute_print(valid_paths, printer_name, None, account_id, is_private=False, print_settings=queued_settings)
        report_print_event(dict(log_entry), queued_settings)
        db.execute("UPDATE print_logs SET status='printed' WHERE id=?", (log_id,))
        db.commit()
        # Clean up files
        for fp in valid_paths:
            try:
                os.remove(fp)
            except OSError:
                pass
        flash(
            f"Job '{log_entry['client']} — {log_entry['purpose']}' sent to printer successfully!",
            "success"
        )
    except Exception as e:
        log_message("ERROR", f"release_job failed for log_id={log_id}: {e}")
        flash(f"Printing failed: {e}", "error")

    return redirect(url_for("my_jobs"))

@app.route("/finish_print/<int:log_id>")
def finish_print(log_id):
    db = get_db()
    log_entry = db.execute("SELECT * FROM print_logs WHERE id = ?", (log_id,)).fetchone()
    if log_entry and log_entry["file_name"]:
        saved_filenames = log_entry["file_name"].split(',')
        for fn in saved_filenames:
            if fn:
                fp = os.path.join(app.config['UPLOAD_FOLDER'], fn)
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except OSError:
                    pass
    return redirect(url_for("print_success", log_id=log_id))

@app.route("/print_success/<int:log_id>")
def print_success(log_id):
    db = get_db()
    log_entry = db.execute("SELECT * FROM print_logs WHERE id = ?", (log_id,)).fetchone()
    if not log_entry:
        return redirect(url_for("employee_form"))
    return render_template("print_success.html", log_entry=log_entry)

@app.route('/preview_file/<filename>')
def preview_file(filename):
    page_range = request.args.get('range', '').strip()
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(file_path):
        return "File not found", 404
        
    if not page_range:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
        
    try:
        pages_to_include = set()
        for part in page_range.split(','):
            part = part.strip()
            if not part: continue
            if '-' in part:
                s, e = part.split('-')
                for p in range(int(s), int(e) + 1):
                    pages_to_include.add(p)
            else:
                pages_to_include.add(int(part))
    except Exception:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
        
    try:
        reader = PyPDF2.PdfReader(file_path)
        writer = PyPDF2.PdfWriter()
        max_pages = len(reader.pages)
        
        sorted_pages = sorted(list(pages_to_include))
        added_any = False
        for p in sorted_pages:
            if 1 <= p <= max_pages:
                writer.add_page(reader.pages[p - 1])
                added_any = True
                
        if not added_any:
            return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
            
        output = io.BytesIO()
        writer.write(output)
        return Response(output.getvalue(), mimetype="application/pdf")
    except Exception:
        return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ---------- API routes ----------

@app.route("/api/employee/<pin>")
def api_get_employee(pin):
    db = get_db()
    employee = db.execute("SELECT name, department FROM employees WHERE employee_code = ?", (pin,)).fetchone()
    if employee:
        return jsonify({"name": employee["name"], "department": employee["department"]})
    return jsonify({"error": "Not found"}), 404

@app.route('/api/network_scan')
def api_network_scan():
    import time
    from concurrent.futures import ThreadPoolExecutor
    import re

    # 1. Determine local network status
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    internet_fine = False
    
    try:
        # Dummy socket connection to detect primary interface IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        internet_fine = True
    except Exception:
        try:
            local_ip = socket.gethostbyname(hostname)
        except Exception:
            pass

    # A connection is considered "fine" if it's not a loopback address or APIPA
    network_fine = (local_ip != "127.0.0.1" and not local_ip.startswith("127.") and not local_ip.startswith("169.254"))

    # Let's double check internet by attempting a quick port 53 connection to Cloudflare DNS
    if not internet_fine:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(("1.1.1.1", 53))
            s.close()
            internet_fine = True
        except Exception:
            internet_fine = False

    # 2. Gather printers from database and system
    printers_to_check = []
    seen_keys = set() # (ip, port) or name

    # A. Get DB configured printers
    try:
        db = get_db()
        db_rows = db.execute("SELECT printer_name, printer_ip, printer_port FROM printer_settings").fetchall()
        for row in db_rows:
            ip = row["printer_ip"].strip() if row["printer_ip"] else None
            port = row["printer_port"] or 9100
            printers_to_check.append({
                "name": row["printer_name"],
                "ip": ip,
                "port": port,
                "source": "configured"
            })
            if ip:
                seen_keys.add((ip, port))
    except Exception as e:
        log_message("ERROR", f"Failed to fetch DB printers for scan: {e}")

    # B. Get System printers
    sys_printers = get_system_printers()
    for sys_name in sys_printers:
        uri = get_printer_device_uri(sys_name)
        ip = None
        port = 9100
        if uri:
            # Parse socket://host:port or similar
            match = re.match(r"[a-zA-Z0-9+.-]+://([^:/]+)(?::(\d+))?", uri)
            if match:
                host = match.group(1)
                port_str = match.group(2)
                if port_str:
                    port = int(port_str)
                # Resolve host if it's not an IP
                if host:
                    try:
                        if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host):
                            ip = host
                        else:
                            ip = socket.gethostbyname(host)
                    except Exception:
                        ip = host
        
        # If we couldn't resolve IP but it's on Windows, try Windows IP resolution
        if not ip and platform.system() == "Windows":
            ip = _resolve_windows_printer_ip(sys_name)

        # Avoid duplicating if the exact same IP and port is already configured
        if ip and (ip, port) in seen_keys:
            continue

        printers_to_check.append({
            "name": sys_name,
            "ip": ip,
            "port": port,
            "source": "system"
        })

    # 3. Connection checking worker
    def check_connection(printer):
        ip = printer["ip"]
        port = printer["port"]
        if not ip or ip == "Local" or ip == "localhost" or ip == "127.0.0.1":
            return {
                **printer,
                "status": "local",
                "latency_ms": None,
                "details": "Local or virtual device (USB/LPT/PDF)."
            }
        
        start_time = time.time()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.8) # 800ms timeout
            s.connect((ip, int(port)))
            s.close()
            latency = int((time.time() - start_time) * 1000)
            return {
                **printer,
                "status": "online",
                "latency_ms": latency,
                "details": f"Online & reachable."
            }
        except socket.timeout:
            return {
                **printer,
                "status": "offline",
                "latency_ms": None,
                "details": "Connection timed out."
            }
        except Exception:
            return {
                **printer,
                "status": "offline",
                "latency_ms": None,
                "details": "Connection refused / port closed."
            }

    # 4. Execute checks in parallel
    results = []
    if printers_to_check:
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(check_connection, printers_to_check))

    # 5. Build summary message
    if network_fine:
        status_msg = f"Your network is fine (IP: {local_ip})."
        if internet_fine:
            status_msg += " Internet is accessible."
    else:
        status_msg = "Your network connection is limited."

    return jsonify({
        "network": {
            "hostname": hostname,
            "ip": local_ip,
            "network_fine": network_fine,
            "internet_fine": internet_fine,
            "status_message": status_msg
        },
        "printers": results
    })


# ---------- Admin routes ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect username or password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    department_filter = request.args.get("department", "all")
    days = request.args.get("days", "30")

    # Sorting
    SORT_COLUMNS = {
        "date": "log_date",
        "employee": "name",
        "department": "department",
        "client": "client",
        "purpose": "purpose",
        "pages": "pages",
    }
    sort_by = request.args.get("sort_by", "date")
    sort_dir = request.args.get("sort_dir", "desc")
    if sort_by not in SORT_COLUMNS:
        sort_by = "date"
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"
    order_col = SORT_COLUMNS[sort_by]
    order_clause = f"{order_col} {'ASC' if sort_dir == 'asc' else 'DESC'}"

    log_search = request.args.get("log_search", "").strip()

    query = "SELECT * FROM print_logs WHERE created_at >= ?"
    params = [_since_cutoff(days)]

    if department_filter != "all":
        query += " AND department = ?"
        params.append(department_filter)

    if log_search:
        query += " AND name LIKE ?"
        params.append(f"%{log_search}%")

    query += f" ORDER BY {order_clause}"
    log_rows = db.execute(query, params).fetchall()

    # Distinct employee names for autocomplete suggestions in the search box
    log_name_suggestions = [r["name"] for r in db.execute(
        "SELECT DISTINCT name FROM print_logs ORDER BY name"
    ).fetchall()]

    # Employee search + sort
    EMP_SORT_COLS = {"name": "name", "department": "department", "status": "banned"}
    emp_search   = request.args.get("emp_search", "").strip()
    emp_sort_by  = request.args.get("emp_sort_by", "name")
    emp_sort_dir = request.args.get("emp_sort_dir", "asc")
    if emp_sort_by  not in EMP_SORT_COLS: emp_sort_by  = "name"
    if emp_sort_dir not in ("asc", "desc"): emp_sort_dir = "asc"
    emp_order = f"{EMP_SORT_COLS[emp_sort_by]} {'ASC' if emp_sort_dir == 'asc' else 'DESC'}"

    emp_query  = "SELECT * FROM employees WHERE name LIKE ?"
    emp_params = [f"%{emp_search}%"]
    emp_query += f" ORDER BY {emp_order}"
    employees = db.execute(emp_query, emp_params).fetchall()

    # Pull managed departments list; fall back to distinct values from employees
    managed_dept_rows = db.execute("SELECT id, name FROM departments ORDER BY name").fetchall()
    managed_depts = [r["name"] for r in managed_dept_rows]
    if not managed_dept_rows:
        managed_depts = [r["department"] for r in db.execute(
            "SELECT DISTINCT department FROM employees ORDER BY department"
        ).fetchall()]

    # {name: id} map for delete buttons in the template
    dept_ids = {r["name"]: r["id"] for r in managed_dept_rows}

    # Departments shown in filter dropdown (union of logs + managed)
    filter_depts = sorted(set(
        [r["department"] for r in db.execute("SELECT DISTINCT department FROM print_logs").fetchall()]
        + managed_depts
    ))

    totals = db.execute(
        """SELECT name, department, employee_code, SUM(pages) as total_pages, COUNT(*) as job_count
           FROM print_logs
           GROUP BY employee_code
           ORDER BY total_pages DESC"""
    ).fetchall()

    dept_totals = db.execute(
        """SELECT department, SUM(pages) as total_pages, COUNT(*) as job_count
           FROM print_logs
           GROUP BY department
           ORDER BY total_pages DESC"""
    ).fetchall()

    printer_settings = db.execute("SELECT * FROM printer_settings").fetchall()
    system_printers  = get_system_printers()

    # --- Monthly summary data for the new dashboard section ---
    start_month = request.args.get("start_month", "").strip()
    end_month   = request.args.get("end_month", "").strip()
    
    available_months = [r["month_val"] for r in db.execute(
        "SELECT DISTINCT substr(log_date, 1, 7) as month_val FROM print_logs WHERE log_date IS NOT NULL ORDER BY month_val DESC"
    ).fetchall() if r["month_val"]]
    
    monthly_query = """
        SELECT substr(log_date, 1, 7) as month_val, SUM(pages) as total_pages, COUNT(*) as job_count
        FROM print_logs
    """
    monthly_params = []
    conditions = []
    if start_month:
        conditions.append("substr(log_date, 1, 7) >= ?")
        monthly_params.append(start_month)
    if end_month:
        conditions.append("substr(log_date, 1, 7) <= ?")
        monthly_params.append(end_month)
        
    if conditions:
        monthly_query += " WHERE " + " AND ".join(conditions)
        
    monthly_query += " GROUP BY month_val ORDER BY month_val DESC"
    monthly_totals = db.execute(monthly_query, monthly_params).fetchall()

    # --- Update history: proof an update actually applied on this machine ---
    update_history = db.execute(
        "SELECT * FROM update_history ORDER BY updated_at DESC LIMIT 20"
    ).fetchall()
    last_update = update_history[0] if update_history else None

    return render_template(
        "admin_dashboard.html",
        logs=log_rows,
        employees=employees,
        departments=filter_depts,
        managed_depts=managed_depts,
        managed_dept_rows=managed_dept_rows,
        dept_ids=dept_ids,
        totals=totals,
        dept_totals=dept_totals,
        department_filter=department_filter,
        days=days,
        sort_by=sort_by,
        sort_dir=sort_dir,
        log_search=log_search,
        log_name_suggestions=log_name_suggestions,
        emp_search=emp_search,
        emp_sort_by=emp_sort_by,
        emp_sort_dir=emp_sort_dir,
        printer_settings=printer_settings,
        system_printers=system_printers,
        monthly_totals=monthly_totals,
        available_months=available_months,
        start_month=start_month,
        end_month=end_month,
        app_version=APP_VERSION,
        update_history=update_history,
        last_update=last_update,
    )


def _since_cutoff(days_str):
    try:
        days = int(days_str)
    except ValueError:
        days = 30
    if days <= 0:
        return "0000-01-01T00:00:00"  # effectively "all time"
    from datetime import timedelta
    return (datetime.now() - timedelta(days=days)).isoformat()


def generate_unique_pin(db):
    """Generate a random 4-digit PIN that doesn't already exist in the DB."""
    existing = {row[0] for row in db.execute("SELECT employee_code FROM employees").fetchall()}
    all_pins = [f"{i:04d}" for i in range(10000)]
    available = [p for p in all_pins if p not in existing]
    if not available:
        return None  # All 10,000 PINs exhausted
    return random.choice(available)


@app.route("/admin/employees/add", methods=["POST"])
@admin_required
def add_employee():
    name = request.form.get("name", "").strip()
    department = request.form.get("department", "").strip()
    custom_code = request.form.get("employee_code", "").strip()
    db = get_db()

    if not name or not department:
        flash("Name and department are required.", "error")
        return redirect(url_for("admin_dashboard"))

    if custom_code:
        # Validate custom code format
        if not custom_code.isdigit() or len(custom_code) != 4:
            flash("Custom Printer Account ID must be exactly 4 digits.", "error")
            return redirect(url_for("admin_dashboard"))
        
        # Check if already exists
        exists = db.execute("SELECT 1 FROM employees WHERE employee_code = ?", (custom_code,)).fetchone()
        if exists:
            flash(f"Printer Account ID '{custom_code}' is already assigned to another employee.", "error")
            return redirect(url_for("admin_dashboard"))
        pin = custom_code
    else:
        pin = generate_unique_pin(db)
        if pin is None:
            flash("No available PINs left (all 10,000 used). Remove old employees first.", "error")
            return redirect(url_for("admin_dashboard"))

    db.execute(
        "INSERT INTO employees (employee_code, name, department, banned, created_at) VALUES (?, ?, ?, 0, ?)",
        (pin, name, department, datetime.now().isoformat())
    )
    db.commit()
    if custom_code:
        flash(f"Added employee {name} with Account ID: {pin}.", "success")
    else:
        flash(f"Added employee {name} — Account ID auto-assigned: {pin}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/departments/add", methods=["POST"])
@admin_required
def add_department():
    name = request.form.get("dept_name", "").strip()
    if not name:
        flash("Department name cannot be empty.", "error")
        return redirect(url_for("admin_dashboard"))
    db = get_db()
    try:
        db.execute("INSERT INTO departments (name) VALUES (?)", (name,))
        db.commit()
        flash(f"Department '{name}' added.", "success")
    except sqlite3.IntegrityError:
        flash(f"Department '{name}' already exists.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/departments/<int:dept_id>/delete", methods=["POST"])
@admin_required
def delete_department(dept_id):
    db = get_db()
    db.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
    db.commit()
    flash("Department removed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/printer_settings/add", methods=["POST"])
@admin_required
def add_printer_setting():
    printer_name = request.form.get("printer_name", "").strip()
    printer_ip   = request.form.get("printer_ip",   "").strip()
    printer_port = request.form.get("printer_port", "9100").strip()

    if not printer_name or not printer_ip:
        flash("Printer name and IP are required.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        port_int = int(printer_port)
    except ValueError:
        port_int = 9100

    db = get_db()
    try:
        db.execute(
            "INSERT OR REPLACE INTO printer_settings (printer_name, printer_ip, printer_port) VALUES (?, ?, ?)",
            (printer_name, printer_ip, port_int)
        )
        db.commit()
        flash(f"Printer '{printer_name}' configured → {printer_ip}:{port_int}", "success")
    except Exception as e:
        flash(f"Error saving printer config: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/printer_settings/<int:setting_id>/delete", methods=["POST"])
@admin_required
def delete_printer_setting(setting_id):
    db = get_db()
    db.execute("DELETE FROM printer_settings WHERE id = ?", (setting_id,))
    db.commit()
    flash("Printer configuration removed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logs/<int:log_id>/delete", methods=["POST"])
@admin_required
def delete_log(log_id):
    db = get_db()
    db.execute("DELETE FROM print_logs WHERE id = ?", (log_id,))
    db.commit()
    flash("Print log entry removed.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/pins.pdf")
@admin_required
def export_pins_pdf():
    db = get_db()
    employees = db.execute(
        "SELECT name, department, employee_code, banned FROM employees ORDER BY name"
    ).fetchall()

    def _build_pdf_buffer():
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=2*cm, rightMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm
        )

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            'PinTitle', parent=styles['Title'],
            fontSize=18, spaceAfter=6, textColor=colors.HexColor('#2f6f4e'), alignment=TA_CENTER
        )
        sub_style = ParagraphStyle(
            'PinSub', parent=styles['Normal'],
            fontSize=10, textColor=colors.HexColor('#5b645f'), alignment=TA_CENTER, spaceAfter=16
        )

        elements = []

        # Logo
        logo_path = os.path.join(os.path.dirname(__file__), 'static', 'img', 'logo.png')
        if os.path.exists(logo_path):
            elements.append(RLImage(logo_path, width=3*cm, height=3*cm))
            elements.append(Spacer(1, 0.3*cm))

        elements.append(Paragraph("Accountable Printing", title_style))
        elements.append(Paragraph(f"Employee PIN List &nbsp;&nbsp;·&nbsp;&nbsp; Generated {datetime.now().strftime('%d %b %Y %H:%M')}", sub_style))

        # Table data
        header = ["#", "Name", "Department", "PIN", "Status"]
        data = [header]
        for i, e in enumerate(employees, 1):
            status = "Blocked" if e["banned"] else "Active"
            data.append([str(i), e["name"], e["department"], e["employee_code"], status])

        col_widths = [1*cm, 6*cm, 5*cm, 2.5*cm, 2.5*cm]
        table = Table(data, colWidths=col_widths, repeatRows=1)
        table.setStyle(TableStyle([
            # Header row
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2f6f4e')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            # Body rows
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('ALIGN', (0, 1), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 1), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f6f5f1')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#dfdcd2')),
            # ROUNDEDCORNERS removed — not supported in all ReportLab versions
        ]))
        elements.append(table)

        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()

    # pywebview's embedded browser cannot handle file downloads (Content-Disposition: attachment).
    # On Windows: write PDF to a temp file and open it with the system PDF viewer instead.
    if platform.system() == "Windows":
        import tempfile
        pdf_bytes = _build_pdf_buffer()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_employee_pins.pdf")
        tmp.write(pdf_bytes)
        tmp.close()
        threading.Thread(target=lambda: os.startfile(tmp.name), daemon=True).start()
        flash("PIN list opened in your PDF viewer — save or print from there.", "success")
        return redirect(url_for("admin_dashboard"))

    # Non-Windows (or browser access): stream the file directly
    return Response(
        _build_pdf_buffer(),
        mimetype='application/pdf',
        headers={'Content-Disposition': 'attachment; filename=employee_pins.pdf'}
    )


@app.route("/admin/monthly_report.pdf")
@admin_required
def export_monthly_report_pdf():
    db = get_db()
    start_month = request.args.get("start_month", "").strip()
    end_month   = request.args.get("end_month", "").strip()
    
    # 1. Fetch aggregated monthly totals for the selected range
    monthly_query = """
        SELECT substr(log_date, 1, 7) as month_val, SUM(pages) as total_pages, COUNT(*) as job_count
        FROM print_logs
    """
    monthly_params = []
    conditions = []
    if start_month:
        conditions.append("substr(log_date, 1, 7) >= ?")
        monthly_params.append(start_month)
    if end_month:
        conditions.append("substr(log_date, 1, 7) <= ?")
        monthly_params.append(end_month)
        
    if conditions:
        monthly_query += " WHERE " + " AND ".join(conditions)
    monthly_query += " GROUP BY month_val ORDER BY month_val DESC"
    
    monthly_totals = db.execute(monthly_query, monthly_params).fetchall()
    
    # 2. Fetch detailed log rows for the selected range
    detail_query = """
        SELECT log_date, name, department, employee_code, client, purpose, pages, file_name
        FROM print_logs
    """
    detail_params = []
    detail_conditions = []
    if start_month:
        detail_conditions.append("substr(log_date, 1, 7) >= ?")
        detail_params.append(start_month)
    if end_month:
        detail_conditions.append("substr(log_date, 1, 7) <= ?")
        detail_params.append(end_month)
        
    if detail_conditions:
        detail_query += " WHERE " + " AND ".join(detail_conditions)
    detail_query += " ORDER BY log_date DESC, id DESC"
    
    detail_rows = db.execute(detail_query, detail_params).fetchall()
    
    # Calculate summary metrics
    total_pages = sum(r["total_pages"] for r in monthly_totals)
    total_jobs  = sum(r["job_count"] for r in monthly_totals)
    avg_pages   = round(total_pages / total_jobs, 1) if total_jobs > 0 else 0
    
    # Find top department and top employee in this range
    top_dept_row = db.execute(f"""
        SELECT department, SUM(pages) as p 
        FROM print_logs 
        {"WHERE " + " AND ".join(detail_conditions) if detail_conditions else ""}
        GROUP BY department ORDER BY p DESC LIMIT 1
    """, detail_params).fetchone()
    top_dept = top_dept_row["department"] if top_dept_row else "N/A"
    
    top_emp_row = db.execute(f"""
        SELECT name, SUM(pages) as p 
        FROM print_logs 
        {"WHERE " + " AND ".join(detail_conditions) if detail_conditions else ""}
        GROUP BY employee_code ORDER BY p DESC LIMIT 1
    """, detail_params).fetchone()
    top_emp = top_emp_row["name"] if top_emp_row else "N/A"

    def _build_pdf_buffer():
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=1.5*cm, rightMargin=1.5*cm,
            topMargin=1.5*cm, bottomMargin=1.5*cm
        )
        styles = getSampleStyleSheet()
        
        title_style = ParagraphStyle(
            'ReportTitle', parent=styles['Title'],
            fontSize=20, spaceAfter=4, textColor=colors.HexColor('#1b4332'), alignment=TA_CENTER
        )
        subtitle_style = ParagraphStyle(
            'ReportSubtitle', parent=styles['Normal'],
            fontSize=10, textColor=colors.HexColor('#40916c'), alignment=TA_CENTER, spaceAfter=20
        )
        h2_style = ParagraphStyle(
            'SectionHeader', parent=styles['Heading2'],
            fontSize=12, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor('#1b4332')
        )
        body_style = ParagraphStyle(
            'Body', parent=styles['Normal'],
            fontSize=9, textColor=colors.HexColor('#2d3748')
        )
        bold_body_style = ParagraphStyle(
            'BoldBody', parent=body_style,
            fontName='Helvetica-Bold'
        )

        elements = []
        
        logo_path = os.path.join(os.path.dirname(__file__), 'static', 'img', 'logo.png')
        if os.path.exists(logo_path):
            elements.append(RLImage(logo_path, width=2.5*cm, height=2.5*cm))
            elements.append(Spacer(1, 0.2*cm))
            
        elements.append(Paragraph("Accountable Printing", title_style))
        
        range_str = "All Time"
        if start_month or end_month:
            if start_month == end_month:
                range_str = f"For Month: {start_month}"
            else:
                range_str = f"From {start_month or 'Beginning'} to {end_month or 'Present'}"
        elements.append(Paragraph(f"Monthly Print Activity Report &nbsp;&nbsp;·&nbsp;&nbsp; {range_str}", subtitle_style))
        
        # 1. Summary Metrics
        elements.append(Paragraph("Executive Summary", h2_style))
        summary_data = [
            [Paragraph("Total Pages Printed:", bold_body_style), Paragraph(str(total_pages), body_style),
             Paragraph("Top Department:", bold_body_style), Paragraph(str(top_dept), body_style)],
            [Paragraph("Total Print Jobs:", bold_body_style), Paragraph(str(total_jobs), body_style),
             Paragraph("Top Employee:", bold_body_style), Paragraph(str(top_emp), body_style)],
            [Paragraph("Average Pages/Job:", bold_body_style), Paragraph(str(avg_pages), body_style),
             Paragraph("", body_style), Paragraph("", body_style)]
        ]
        summary_table = Table(summary_data, colWidths=[4*cm, 4.5*cm, 4*cm, 5.5*cm])
        summary_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f4f9f4')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('PADDING', (0, 0), (-1, -1), 6),
            ('LINEBELOW', (0, 0), (-1, -1), 0.5, colors.HexColor('#d8f3dc')),
        ]))
        elements.append(summary_table)
        elements.append(Spacer(1, 0.4*cm))
        
        # 2. Monthly Summary
        elements.append(Paragraph("Monthly Aggregations", h2_style))
        agg_data = [["Month", "Total Jobs", "Total Pages"]]
        for row in monthly_totals:
            agg_data.append([row["month_val"], str(row["job_count"]), str(row["total_pages"])])
        
        agg_table = Table(agg_data, colWidths=[6*cm, 6*cm, 6*cm])
        agg_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2d6a4f')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('TOPPADDING', (0, 0), (-1, 0), 6),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e9ecef')),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('PADDING', (0, 1), (-1, -1), 5),
        ]))
        elements.append(agg_table)
        elements.append(Spacer(1, 0.5*cm))
        
        # 3. Detailed Logs
        elements.append(Paragraph("Detailed Print Activity Log", h2_style))
        if detail_rows:
            log_data = [["Date", "Employee", "Department", "Client / Purpose", "Pages"]]
            for r in detail_rows:
                emp_name = f"{r['name']} ({r['employee_code']})"
                purpose_str = f"{r['client']} - {r['purpose']}"
                log_data.append([
                    r["log_date"],
                    Paragraph(emp_name, body_style),
                    Paragraph(r["department"], body_style),
                    Paragraph(purpose_str, body_style),
                    str(r["pages"])
                ])
            
            log_table = Table(log_data, colWidths=[2.2*cm, 4*cm, 3.3*cm, 7*cm, 1.5*cm])
            log_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#40916c')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('ALIGN', (4, 0), (4, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 9),
                ('TOPPADDING', (0, 0), (-1, 0), 6),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8f9fa')]),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e9ecef')),
                ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('PADDING', (0, 1), (-1, -1), 4),
            ]))
            elements.append(log_table)
        else:
            elements.append(Paragraph("No log details found for this period.", body_style))
            
        doc.build(elements)
        buffer.seek(0)
        return buffer.getvalue()

    if platform.system() == "Windows":
        import tempfile
        pdf_bytes = _build_pdf_buffer()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="_monthly_print_report.pdf")
        tmp.write(pdf_bytes)
        tmp.close()
        threading.Thread(target=lambda: os.startfile(tmp.name), daemon=True).start()
        flash("Monthly print activity report opened in PDF viewer.", "success")
        return redirect(url_for("admin_dashboard", start_month=start_month, end_month=end_month))
        
    return Response(
        _build_pdf_buffer(),
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename=monthly_print_report.pdf'}
    )

@app.route("/admin/employees/<employee_code>/delete", methods=["POST"])
@admin_required
def delete_employee(employee_code):
    """Permanently remove an employee. Their past print_logs history is kept
    (employee_code is stored as plain text there, not a foreign key), but they
    will no longer be able to log in or submit new print jobs with this PIN."""
    db = get_db()
    employee = db.execute(
        "SELECT * FROM employees WHERE employee_code = ?", (employee_code,)
    ).fetchone()
    if not employee:
        flash("Employee not found.", "error")
        return redirect(url_for("admin_dashboard"))

    db.execute("DELETE FROM employees WHERE employee_code = ?", (employee_code,))
    db.commit()
    flash(f"{employee['name']} ({employee_code}) has been deleted.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/<employee_code>/edit_code", methods=["POST"])
@admin_required
def edit_employee_code(employee_code):
    new_code = request.form.get("new_code", "").strip()
    db = get_db()

    # Validate format
    if not new_code.isdigit() or len(new_code) != 4:
        flash("Account ID must be exactly 4 digits.", "error")
        return redirect(url_for("admin_dashboard"))

    # Same as current — no change needed
    if new_code == employee_code:
        flash("That is already their current Account ID — no change made.", "warning")
        return redirect(url_for("admin_dashboard"))

    # Check if the new code is already taken by someone else
    existing = db.execute(
        "SELECT name FROM employees WHERE employee_code = ? AND employee_code != ?",
        (new_code, employee_code)
    ).fetchone()

    if existing:
        flash(
            f"Account ID {new_code} is already assigned to '{existing['name']}'. "
            "Please choose a different ID.",
            "error"
        )
        return redirect(url_for("admin_dashboard"))

    # Get the employee's name for the flash message
    emp = db.execute("SELECT name FROM employees WHERE employee_code = ?", (employee_code,)).fetchone()

    # Update the employee record
    db.execute(
        "UPDATE employees SET employee_code = ? WHERE employee_code = ?",
        (new_code, employee_code)
    )
    # Also update any existing print logs so history stays linked
    db.execute(
        "UPDATE print_logs SET employee_code = ? WHERE employee_code = ?",
        (new_code, employee_code)
    )
    db.commit()

    flash(
        f"Account ID for '{emp['name']}' changed from {employee_code} → {new_code}.",
        "success"
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/report.csv")
@admin_required
def export_report():
    db = get_db()

    # Honour the same filters/sort currently visible on the dashboard
    department_filter = request.args.get("department", "all")
    days = request.args.get("days", "30")
    SORT_COLUMNS = {
        "date": "log_date", "employee": "name", "department": "department",
        "client": "client", "purpose": "purpose", "pages": "pages",
    }
    sort_by  = request.args.get("sort_by", "date")
    sort_dir = request.args.get("sort_dir", "desc")
    if sort_by  not in SORT_COLUMNS: sort_by  = "date"
    if sort_dir not in ("asc", "desc"): sort_dir = "desc"
    order_col    = SORT_COLUMNS[sort_by]
    order_clause = f"{order_col} {'ASC' if sort_dir == 'asc' else 'DESC'}"

    query  = ("SELECT employee_code, name, department, log_date, pages, client, purpose, created_at"
              " FROM print_logs WHERE created_at >= ?")
    params = [_since_cutoff(days)]
    if department_filter != "all":
        query += " AND department = ?"
        params.append(department_filter)
    log_search = request.args.get("log_search", "").strip()
    if log_search:
        query += " AND name LIKE ?"
        params.append(f"%{log_search}%")
    query += f" ORDER BY {order_clause}"

    rows = db.execute(query, params).fetchall()

    HEADERS = ["Employee Code", "Name", "Department", "Date", "Pages", "Client", "Purpose", "Logged At"]

    def _write_rows(w):
        w.writerow(HEADERS)
        for r in rows:
            w.writerow([r["employee_code"], r["name"], r["department"], r["log_date"],
                        r["pages"], r["client"], r["purpose"], r["created_at"]])

    # pywebview's embedded browser can't download files directly.
    # Write to a temp file and open it with the default system app instead.
    if platform.system() == "Windows":
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix="_print_usage_report.csv",
            mode='w', newline='', encoding='utf-8'
        )
        _write_rows(csv.writer(tmp))
        tmp.close()
        threading.Thread(target=lambda: os.startfile(tmp.name), daemon=True).start()
        flash("CSV report opened — save it from the application that opened.", "success")
        return redirect(url_for("admin_dashboard"))

    output = io.StringIO()
    _write_rows(csv.writer(output))
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=print_usage_report.csv"},
    )


# ---------- Auto-update (GitHub Releases) ----------

def _parse_version(v):
    v = (v or "").strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_update():
    """Query GitHub Releases for the latest published version.
    Never raises — returns a dict, or None if the check itself failed
    (e.g. offline). Safe to call on every app launch."""
    try:
        url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "AccountablePrintingApp"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        latest_tag = data.get("tag_name", "")
        if not latest_tag:
            return None

        download_url = None
        for asset in data.get("assets", []):
            if asset.get("name", "").lower().endswith(".exe"):
                download_url = asset.get("browser_download_url")
                break

        is_newer = _parse_version(latest_tag) > _parse_version(APP_VERSION)
        return {
            "update_available": bool(is_newer and download_url),
            "current_version": APP_VERSION,
            "latest_version": latest_tag.lstrip("vV"),
            "download_url": download_url,
            "notes": (data.get("body") or "")[:500],
        }
    except Exception as e:
        log_message("WARNING", f"Update check failed (probably offline): {e}")
        return None


@app.route("/api/check_update")
def api_check_update():
    result = check_for_update()
    if result is None:
        return jsonify({"update_available": False})
    return jsonify(result)


@app.route("/api/install_update", methods=["POST"])
def api_install_update():
    """Downloads the new installer and launches it silently, then exits this
    app so the installer can overwrite the running files."""
    data = request.get_json(silent=True) or {}
    download_url = data.get("download_url")
    if not download_url:
        return jsonify({"success": False, "error": "no_download_url"}), 400

    try:
        installer_path = os.path.join(tempfile.gettempdir(), "AccountablePrinting_Update.exe")
        log_message("INFO", f"Downloading update from {download_url}")
        urllib.request.urlretrieve(download_url, installer_path)
    except Exception as e:
        log_message("ERROR", f"Update download failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

    def _launch_installer_and_quit():
        time.sleep(1)  # give the JS on the client a moment to show the "restarting" message
        try:
            # /VERYSILENT + /CLOSEAPPLICATIONS lets Inno Setup close this exe itself and reopen it after
            subprocess.Popen(
                [installer_path, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                **_NO_WINDOW
            )
        except Exception as e:
            log_message("ERROR", f"Failed to launch installer: {e}")
        os._exit(0)

    threading.Thread(target=_launch_installer_and_quit, daemon=True).start()
    return jsonify({"success": True})


def start_server():
    # The server must run without the reloader when embedded in a native window
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)

if __name__ == "__main__":
    init_db()
    record_app_version()
    
    # Start the Flask server in a background thread
    server_thread = threading.Thread(target=start_server)
    server_thread.daemon = True
    server_thread.start()
    
    if platform.system() == "Windows":
        # Create and launch the native desktop GUI window (Requires Windows or QT/GTK)
        icon_path = os.path.join(os.path.dirname(__file__), 'icon.ico')
        webview.create_window(
            "Accountable Printing", "http://127.0.0.1:5000/",
            width=1000, height=800,
            resizable=False
        )
        webview.start(icon=icon_path if os.path.exists(icon_path) else None)
    else:
        # On Linux/Kali, missing Qt/GTK can cause pywebview to fail.
        # Fall back to default browser for development testing.
        import webbrowser
        import time
        print("Running on Linux. Opening in default browser for testing...")
        time.sleep(1.25)
        webbrowser.open_new("http://127.0.0.1:5000/")
        
        # Keep the main thread alive since the server thread is a daemon
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
