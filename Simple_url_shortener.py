from flask import Flask  
from flask import request 
from flask import jsonify  
from flask import redirect  
from flask import render_template_string

import os

import sqlite3  
import secrets  
import string  
import time  
import threading 
from collections import defaultdict, deque 
from urllib.parse import urlparse 

from datetime import datetime, timedelta  

app = Flask(__name__) 
# simple frontend (so opening doesn't show "Not Found")
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

DB_NAME = os.getenv("SHORTLY_DB", "urls.db")  

# rate limiting prevents spam / brute forcing short ids (reduces time complexity increase in efficiency)
RATE_LIMIT = 30  
RATE_WINDOW_SEC = 60  
request_log = defaultdict(deque)  # stores timestamps per ip


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

def get_db():
    # new connection per request helps avoid locked transactions in sqlite
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # allows dict-like access

    # WAL mode -> reads don't block writes
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS urls (
            id TEXT PRIMARY KEY,
            org_url TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT
        )
    """)

    cursor.execute("PRAGMA table_info(urls)")
    columns = [col[1] for col in cursor.fetchall()]

    # expiry column already handled in your old version, keeping it
    if "expires_at" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN expires_at TEXT")
        conn.commit()

    if "click_count" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN click_count INTEGER DEFAULT 0")
        conn.commit()

    if "last_accessed" not in columns:
        cursor.execute("ALTER TABLE urls ADD COLUMN last_accessed TEXT")
        conn.commit()
      
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_org_url ON urls(org_url)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_urls_expires_at ON urls(expires_at)")

    conn.commit()
    conn.close()


init_db()  

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
    # if already exist, just increase length
    return generate_short_url(length + 1)

def normalize_url(org_url):
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


@app.route("/shorten", methods=["POST"])  # accepts post request to shorten url
def shorten_url():
    conn = get_db()  
    cursor = conn.cursor()

    data = request.get_json(silent=True) or {}  # safer if body is empty
    org_url_raw = data.get("org_url")  
    expiry_days_raw = data.get("expiry_days", 7)  

    try:
        org_url = normalize_url(org_url_raw)
        expiry_days = parse_expiry_days(expiry_days_raw)
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    # check duplicate
    cursor.execute("SELECT id, expires_at FROM urls WHERE org_url=?", (org_url,))
    existing = cursor.fetchone()

    now = datetime.now()
    expiry_date = (now + timedelta(days=expiry_days)).strftime("%Y-%m-%d %H:%M:%S.%f")

    if existing:
        short_id = existing["id"]
        existing_exp = existing["expires_at"]

        # if expired already, replace it with a new one
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
            # extend expiry on reuse 
            cursor.execute("UPDATE urls SET expires_at=? WHERE id=?", (expiry_date, short_id))
            conn.commit()
            conn.close()
            base_url = request.host_url.rstrip("/")   # auto-detects host (localhost / render / railway)
            return jsonify({"short_url": f"{base_url}/{short_id}", "expires_at": expiry_date}), 200

    short_id = generate_unique_short_id(conn, length=7)

    cursor.execute(
        "INSERT INTO urls (id, org_url, expires_at, click_count) VALUES (?, ?, ?, ?)",
        (short_id, org_url, expiry_date, 0)
    )
    conn.commit()
    conn.close()

    base_url = request.host_url.rstrip("/")   # auto-detects host (localhost / render / railway)
    return jsonify({"short_url": f"{base_url}/{short_id}", "expires_at": expiry_date}), 201


@app.route("/<short_id>", methods=["GET"])  # get request for shortened url
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
            return jsonify({"error": "Short URL expired"}), 41
          
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
    cursor.execute("""
        UPDATE urls
        SET click_count = click_count + 1,
            last_accessed = ?
        WHERE id = ?
    """, (now_str, short_id))
    conn.commit()
    conn.close()

    return redirect(original_url)  # redirect if still valid (weblink with multiple redirection)


@app.route("/api/info/<short_id>", methods=["GET"])  
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


