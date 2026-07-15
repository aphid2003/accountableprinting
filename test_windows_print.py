"""
Run this on the Windows machine to diagnose the printer connection.
Double-click or run: py test_windows_print.py
"""
import subprocess
import socket
import json
import sys

PRINTER_IP   = "192.168.100.250"
PRINTER_PORT = 9100
ACCOUNT_ID   = "5864"  # Change to your account ID

print("=" * 60)
print("  Accountable Printing — Windows Diagnostic")
print("=" * 60)

# ── Step 1: List all printers ────────────────────────────────
print("\n[1] Installed printers:")
try:
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "Get-Printer | Select-Object Name, PortName | Format-Table -AutoSize"],
        timeout=10
    ).decode(errors="replace")
    print(out)
except Exception as e:
    print(f"   ERROR: {e}")

# ── Step 2: Get port info for each printer ───────────────────
print("\n[2] Printer ports:")
try:
    out = subprocess.check_output(
        ["powershell", "-NoProfile", "-Command",
         "Get-PrinterPort | Select-Object Name, PrinterHostAddress, PortNumber | Format-Table -AutoSize"],
        timeout=10
    ).decode(errors="replace")
    print(out)
except Exception as e:
    print(f"   ERROR: {e}")

# ── Step 3: Test TCP connection to printer ───────────────────
print(f"\n[3] Testing TCP connection to {PRINTER_IP}:{PRINTER_PORT} ...")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    result = s.connect_ex((PRINTER_IP, PRINTER_PORT))
    s.close()
    if result == 0:
        print(f"   ✅  Port {PRINTER_PORT} is OPEN — printer is reachable!")
    else:
        print(f"   ❌  Port {PRINTER_PORT} is CLOSED or blocked (error code {result})")
        print("   → Check Windows Firewall or that you're on the same network.")
except Exception as e:
    print(f"   ❌  Connection error: {e}")

# ── Step 4: Send Hello World test print ─────────────────────
print(f"\n[4] Sending Hello World test page (account={ACCOUNT_ID}) ...")
try:
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("Helvetica-Bold", 36)
    c.drawCentredString(297, 420, "Hello World from Windows!")
    c.setFont("Helvetica", 16)
    c.drawCentredString(297, 370, f"Account ID: {ACCOUNT_ID}")
    c.save()
    pdf_data = buf.getvalue()

    uel = b"\x1b%-12345X"
    payload = (
        uel + b"@PJL JOB NAME=\"WinTest\"\r\n"
        b"@PJL SET JOBATTR=\"ACNT=" + ACCOUNT_ID.encode() + b"\"\r\n"
        b"@PJL ENTER LANGUAGE=PDF\r\n"
        + pdf_data
        + uel + b"@PJL EOJ\r\n" + uel
    )

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(15)
    s.connect((PRINTER_IP, PRINTER_PORT))
    s.sendall(payload)
    s.close()
    print(f"   ✅  Sent {len(payload)} bytes — check the printer now!")

except ImportError:
    print("   ⚠️  reportlab not installed. Install with: py -m pip install reportlab")
    print("   Trying raw text fallback...")
    try:
        uel = b"\x1b%-12345X"
        raw_text = b"Hello World from Windows!\r\n\f"
        payload = (
            uel + b"@PJL JOB NAME=\"WinTest\"\r\n"
            b"@PJL SET JOBATTR=\"ACNT=" + ACCOUNT_ID.encode() + b"\"\r\n"
            b"@PJL ENTER LANGUAGE=PCL\r\n"
            + raw_text
            + uel + b"@PJL EOJ\r\n" + uel
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect((PRINTER_IP, PRINTER_PORT))
        s.sendall(payload)
        s.close()
        print(f"   ✅  Sent raw PCL text ({len(payload)} bytes) — check the printer!")
    except Exception as e2:
        print(f"   ❌  Raw send also failed: {e2}")
except Exception as e:
    print(f"   ❌  Failed to send: {e}")

print("\n" + "=" * 60)
print("  Done. Paste the output above if something didn't work.")
print("=" * 60)
input("\nPress Enter to exit...")
