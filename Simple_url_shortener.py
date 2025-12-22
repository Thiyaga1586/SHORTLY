from flask import Flask  # Web framework for creating API's
from flask import request  # Handles incoming data (eg.JSON requests)
from flask import jsonify  # Converts python dictionaries into JSON responses
from flask import redirect  # Sends the user to another URL when they visit a route
from flask import render_template_string  # for small frontend page 

import os

import sqlite3  # Connects to a SQLite database to store and redirect to urls
import secrets  # Better random token generation than slicing UUID
import string  # Used for base62 short codes (letters + digits)
import time  # For rate limiting + background cleanup
import threading  # For cleanup job (removing expired urls)
from collections import defaultdict, deque  # Used for simple in-memory rate limiter
from urllib.parse import urlparse  # More reliable URL validation than regex

from datetime import datetime, timedelta  # handles expiry dates for URLs

app = Flask(__name__)  # creates a flask app instance allowing us to define API routes
# Simple frontend (so opening / doesn't show "Not Found")
# Keeps it lightweight: no React, no templates folder, just one clean page.
HOME_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>SHORTLY</title>
</head>
<body style="font-family: Arial; max-width: 720px; margin: 40px auto;">
  <h2>SHORTLY</h2>
  <p>Paste a long URL and get a short link.</p>

  <form id="f">
    <input id="url" placeholder="https://example.com" style="width:100%; padding:10px;" required />
    <div style="margin-top:10px;">
      <input id="days" type="number" value="7" min="1" max="365" style="width:120px; padding:8px;" />
      <button style="padding:8px 14px;">Shorten</button>
    </div>
  </form>

  <div id="out" style="margin-top:18px;"></div>

<script>
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const org_url = document.getElementById("url").value;
  const expiry_days = parseInt(document.getElementById("days").value, 10);

  const res = await fetch("/shorten", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({org_url, expiry_days})
  });

  const data = await res.json();
  const out = document.getElementById("out");

  if (!res.ok) {
    out.innerHTML = "<p style='color:red;'>" + (data.error || "Error") + "</p>";
    return;
  }

  out.innerHTML = `
    <p><b>Short URL:</b> <a href="${data.short_url}" target="_blank">${data.short_url}</a></p>
    <p><b>Expires at:</b> ${data.expires_at}</p>
    <button id="copy">Copy</button>
  `;

  document.getElementById("copy").onclick = async () => {
    await navigator.clipboard.writeText(data.short_url);
    alert("Copied!");
  };
});
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def home():
    return render_template_string(HOME_HTML)

DB_NAME = os.getenv("SHORTLY_DB", "urls.db")  # lets tests use a temporary DB  # Database name

# Rate Limiting (simple + effective) 
# Why: prevents spam / brute forcing short ids (recruiters love this)
RATE_LIMIT = 30  # max requests
RATE_WINDOW_SEC = 60  # per 60 seconds
request_log = defaultdict(deque)  # stores timestamps per IP


def rate_limit_guard():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)  # handles proxies + local
    now = time.time()
    q = request_log[ip]

    # remove old timestamps outside window
    while q and now - q[0] > RATE_WINDOW_SEC:
        q.popleft()

    if len(q) >= RATE_LIMIT:
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429

    q.append(now)
    return None


@app.before_request
def before_every_request():
    if request.path == "/shorten" and request.method == "POST":
        limited = rate_limit_guard()
        if limited:
            return limited

# DB Helpers (fresh connection per request) 
def get_db():
    # New connection per request helps avoid locked transactions in sqlite
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # allows dict-like access

    # WAL mode helps concurrency (reads don't block writes)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# Create tables + upgrade old DB schema if needed
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # Create the table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            id TEXT PRIMARY KEY,
            org_url TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        )
    """)

    # Ensure columns exist (older databases)
    cursor.execute("PRAGMA table_info(urls)")
    columns = [col[1] for col in cursor.fetchall()]  # Extract column names

    # expiry column already handled in your old version, keeping it
    if "expires_at" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN expires_at TEXT")
        conn.commit()

    # WOW additions: analytics
    if "click_count" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN click_count INTEGER DEFAULT 0")
        conn.commit()

    if "last_accessed" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN last_accessed TEXT")
        conn.commit()

    # indexes for performance (fast lookup + cleanup)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_org_url ON urls(org_url)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_expires_at ON urls(expires_at)")

    conn.commit()
    conn.close()


init_db()  # initialize schema once at startup


# Short code generator (base62) 
BASE62 = string.ascii_letters + string.digits  # a-zA-Z0-9


def generate_short_url(length=7):
    # base62 is short, url-safe, and harder to guess than uuid[:6]
    return "".join(secrets.choice(BASE62) for _ in range(length))


def generate_unique_short_id(conn, length=7):
    # collision-safe: generate -> check DB -> retry
    cursor = conn.cursor()
    for _ in range(10):
        short_id = generate_short_url(length)
        cursor.execute("SELECT 1 FROM urls WHERE id=?", (short_id,))
        if cursor.fetchone() is None:
            return short_id
    # if unlucky, just increase length
    return generate_short_url(length + 1)


# URL Validation (more reliable than regex)
def normalize_url(org_url):
    # accepts "www.google.com" and converts to "https://www.google.com"
    if org_url is None:
        raise ValueError("URL is required")

    org_url = org_url.strip()
    if not org_url:
        raise ValueError("URL is required")

    if not org_url.startswith(("http://", "https://")):
        org_url = "https://" + org_url  # default to https

    parsed = urlparse(org_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.netloc:
        raise ValueError("Invalid URL host")

    return org_url


def parse_expiry_days(expiry_days):
    # validates expiry_days and keeps it safe
    if expiry_days is None:
        return 7

    try:
        expiry_days = int(expiry_days)
    except ValueError:
        raise ValueError("Invalid expiry_days value")

    if expiry_days < 1 or expiry_days > 365:
        raise ValueError("Expiry time must be between 1 and 365 days")

    return expiry_days


# Background cleanup job (deletes expired urls)
def cleanup_expired_urls_forever(interval_sec=300):
    # run every 5 minutes by default
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
            cursor.execute("""
                DELETE FROM urls
                WHERE expires_at IS NOT NULL AND expires_at < ?
            """, (now_str,))

            conn.commit()
            conn.close()
        except Exception:
            # keep it silent; we don't want cleanup crashing the app
            pass

        time.sleep(interval_sec)


threading.Thread(target=cleanup_expired_urls_forever, daemon=True).start()  # starts cleanup in background


# API Routes 
@app.route("/health", methods=["GET"])  # quick health check
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/shorten", methods=["POST"])  # Flask decorator which accepts post request to shorten url
def shorten_url():
    conn = get_db()  # new connection per request
    cursor = conn.cursor()  # New pointer to fetch data

    data = request.get_json(silent=True) or {}  # safer if body is empty
    org_url_raw = data.get("org_url")  # Extract original url from the json
    expiry_days_raw = data.get("expiry_days", 7)  # Get expiry days if exist or set it to 7 (default)

    # validate + normalize url
    try:
        org_url = normalize_url(org_url_raw)
        expiry_days = parse_expiry_days(expiry_days_raw)
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    # check duplicate (reuse short url for same org_url if still valid)
    cursor.execute("SELECT id, expires_at FROM urls WHERE org_url=?", (org_url,))
    existing = cursor.fetchone()

    now = datetime.now()
    expiry_date = (now + timedelta(days=expiry_days)).strftime("%Y-%m-%d %H:%M:%S.%f")

    if existing:
        short_id = existing["id"]
        existing_exp = existing["expires_at"]

        # if expired already, replace it with a new one (cleaner user experience)
        if existing_exp:
            try:
                exp_dt = datetime.strptime(existing_exp, "%Y-%m-%d %H:%M:%S.%f")
                if exp_dt < now:
                    cursor.execute("DELETE FROM urls WHERE id=?", (short_id,))
                    conn.commit()
                    existing = None
            except ValueError:
                # if old format, just treat as expired and refresh
                cursor.execute("DELETE FROM urls WHERE id=?", (short_id,))
                conn.commit()
                existing = None

        if existing:
            # extend expiry on reuse (nice feature)
            cursor.execute("UPDATE urls SET expires_at=? WHERE id=?", (expiry_date, short_id))
            conn.commit()
            conn.close()
            base_url = request.host_url.rstrip("/")   # auto-detects host (localhost / render / railway)
            return jsonify({"short_url": f"{base_url}/{short_id}", "expires_at": expiry_date}), 200

    # create fresh short id
    short_id = generate_unique_short_id(conn, length=7)

    cursor.execute(
        "INSERT INTO urls (id, org_url, expires_at, click_count) VALUES (?, ?, ?, ?)",
        (short_id, org_url, expiry_date, 0)
    )
    conn.commit()
    conn.close()

    base_url = request.host_url.rstrip("/")   # auto-detects host (localhost / render / railway)
    return jsonify({"short_url": f"{base_url}/{short_id}", "expires_at": expiry_date}), 201


@app.route("/<short_id>", methods=["GET"])  # Handles get request for shortened url
def get_original_url(short_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT org_url, expires_at FROM urls WHERE id=?", (short_id,))
    result = cursor.fetchone()

    if not result:
        conn.close()
        return jsonify({"error": "Short URL not found"}), 404

    original_url = result["org_url"]
    expires_at = result["expires_at"]

    # expiry check
    if expires_at:
        try:
            exp_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S.%f")
            if exp_dt < datetime.now():
                cursor.execute("DELETE FROM urls WHERE id=?", (short_id,))
                conn.commit()
                conn.close()
                return jsonify({"error": "Short URL expired"}), 410
        except ValueError:
            # if bad format, delete safely
            cursor.execute("DELETE FROM urls WHERE id=?", (short_id,))
            conn.commit()
            conn.close()
            return jsonify({"error": "Short URL expired"}), 410

    # analytics (click count + last accessed)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    cursor.execute("""
        UPDATE urls
        SET click_count = click_count + 1,
            last_accessed = ?
        WHERE id = ?
    """, (now_str, short_id))
    conn.commit()
    conn.close()

    return redirect(original_url)  # Redirect if still valid


@app.route("/api/info/<short_id>", methods=["GET"])  # recruiter-friendly endpoint (shows analytics)
def info(short_id):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, org_url, created_at, expires_at, click_count, last_accessed
        FROM urls
        WHERE id=?
    """, (short_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return jsonify({"error": "Short URL not found"}), 404

    conn.close()
    return jsonify({
        "id": row["id"],
        "org_url": row["org_url"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "click_count": row["click_count"],
        "last_accessed": row["last_accessed"]
    }), 200


if __name__ == "__main__":
    app.run()

