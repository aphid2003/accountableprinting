"""
Central Print Monitoring API
-----------------------------
This is a small, separate service — NOT part of the desktop app. Deploy it
once, on any always-on host (a $5-10/mo VM, or a free tier on Render/Railway/
Fly.io). Every desktop app POSTs a print event here after each print job;
this service is the only thing that talks to MongoDB Atlas directly.

Why a central API instead of each desktop app talking to Atlas directly:
  - You never ship your MongoDB connection string to 100+ employee machines.
  - You can revoke one device's access (rotate its key) without touching Atlas.
  - You can rate-limit / validate input before it hits your database.

Run locally:
    pip install -r requirements.txt
    cp .env.example .env   # then fill in MONGODB_URI and keys
    python server.py

Deploy: see README.md in this folder.
"""

import os
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify
from pymongo import MongoClient, DESCENDING
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI", "")
DB_NAME = os.environ.get("MONGODB_DB", "print_monitoring")

# Comma-separated list of API keys that desktop apps are allowed to use.
# Generate a long random string per device (or one shared key to start).
DEVICE_API_KEYS = set(k.strip() for k in os.environ.get("DEVICE_API_KEYS", "").split(",") if k.strip())

# Separate key for the admin/monitoring dashboard reads (keep this one private).
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and fill it in.")

client = MongoClient(MONGODB_URI)
db = client[DB_NAME]
events = db["print_events"]
events.create_index([("printed_at", DESCENDING)])
events.create_index("device_id")
events.create_index("employee_code")
events.create_index("department")


def _device_authorized():
    key = request.headers.get("X-API-Key", "")
    # If no keys are configured yet, allow through (useful for first local test)
    # but this should never be true in production — set DEVICE_API_KEYS.
    if not DEVICE_API_KEYS:
        return True
    return key in DEVICE_API_KEYS


def _admin_authorized():
    key = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_API_KEY) and key == ADMIN_API_KEY


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/api/v1/print-events", methods=["POST"])
def create_print_event():
    """Called by each desktop app right after a print job completes."""
    if not _device_authorized():
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    required = ["device_id", "employee_code", "printed_at"]
    if not all(body.get(k) for k in required):
        return jsonify({"error": "missing_fields", "required": required}), 400

    doc = {
        "_id": str(uuid.uuid4()),
        "device_id": body.get("device_id"),
        "employee_code": body.get("employee_code"),
        "name": body.get("name"),
        "department": body.get("department"),
        "client": body.get("client"),
        "purpose": body.get("purpose"),
        "pages": body.get("pages"),
        "printer": body.get("printer"),
        "print_settings": body.get("print_settings") or {},
        "printed_at": body.get("printed_at"),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    events.insert_one(doc)
    return jsonify({"success": True}), 201


@app.route("/api/v1/print-events", methods=["GET"])
def list_print_events():
    """Used by the admin monitoring dashboard. Requires X-Admin-Key."""
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    limit = min(int(request.args.get("limit", 200)), 1000)
    query = {}
    if request.args.get("department"):
        query["department"] = request.args["department"]
    if request.args.get("employee_code"):
        query["employee_code"] = request.args["employee_code"]
    if request.args.get("device_id"):
        query["device_id"] = request.args["device_id"]

    cursor = events.find(query).sort("printed_at", DESCENDING).limit(limit)
    return jsonify({"events": list(cursor)})


@app.route("/api/v1/print-events/summary", methods=["GET"])
def summary():
    """Aggregated totals by department, for a quick company-wide overview."""
    if not _admin_authorized():
        return jsonify({"error": "unauthorized"}), 401

    pipeline = [
        {"$group": {
            "_id": "$department",
            "jobs": {"$sum": 1},
            "pages": {"$sum": "$pages"},
        }},
        {"$sort": {"pages": -1}},
    ]
    return jsonify({"by_department": list(events.aggregate(pipeline))})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
