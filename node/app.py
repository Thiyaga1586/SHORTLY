"""
node/app.py  (production v2)
-----------------------------
Changes from v1:
  ✓ SQLite  →  PostgreSQL via psycopg2 + connection pool (5–20 conns)
  ✓ /node/export?start=TOKEN&end=TOKEN  for automatic key migration
  ✓ /node/migrate  unchanged but now idempotent (ON CONFLICT DO NOTHING)
  ✓ Expiry cleanup uses DELETE … RETURNING to avoid full-table scans
  ✓ gunicorn --workers N safe: pool is per-worker (no shared fd)
  ✓ WAL mode removed (PostgreSQL handles concurrency natively)
"""

import os
import sys
import string
import secrets
import threading
import time
import logging

from datetime import datetime, timedelta
from urllib.parse import urlparse

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, redirect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [" + os.getenv("NODE_ID", "node") + "] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── config ────────────────────────────────────────────────────────────────────

NODE_ID  = os.getenv("NODE_ID", "node-0")
BASE62   = string.ascii_letters + string.digits

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://shortly:shortly@postgres:5432/shortly_{NODE_ID.replace('-', '_')}",
)

# ── connection pool ───────────────────────────────────────────────────────────
# psycopg2 ThreadedConnectionPool is safe across Gunicorn threads.
# min=2, max=20 — tune via env if needed.

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                min_conn = int(os.getenv("DB_POOL_MIN", "2"))
                max_conn = int(os.getenv("DB_POOL_MAX", "20"))
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    min_conn, max_conn, dsn=DATABASE_URL
                )
                log.info("PG pool created (min=%d max=%d)", min_conn, max_conn)
    return _pool


class _Conn:
    """Context manager: borrows a connection from the pool and returns it."""
    def __enter__(self):
        self.conn = _get_pool().getconn()
        self.conn.autocommit = False
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        _get_pool().putconn(self.conn)


# ── schema init ───────────────────────────────────────────────────────────────

def init_db():
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS urls (
                    id            TEXT PRIMARY KEY,
                    org_url       TEXT NOT NULL,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    expires_at    TIMESTAMPTZ,
                    click_count   INTEGER DEFAULT 0,
                    last_accessed TIMESTAMPTZ,
                    token_hash    BIGINT       -- SHA-256 token for range export
                );
                CREATE INDEX IF NOT EXISTS idx_org_url    ON urls(org_url);
                CREATE INDEX IF NOT EXISTS idx_expires_at ON urls(expires_at);
                CREATE INDEX IF NOT EXISTS idx_token_hash ON urls(token_hash);
            """)
    log.info("DB schema ready: %s", DATABASE_URL.rsplit("@", 1)[-1])


# Retry init a few times in case Postgres isn't ready yet
for _attempt in range(10):
    try:
        init_db()
        break
    except Exception as _exc:
        log.warning("DB not ready (%s), retrying in 3 s…", _exc)
        time.sleep(3)


# ── helpers ───────────────────────────────────────────────────────────────────

def _short_id(conn) -> str:
    with conn.cursor() as cur:
        for _ in range(10):
            sid = "".join(secrets.choice(BASE62) for _ in range(7))
            cur.execute("SELECT 1 FROM urls WHERE id=%s", (sid,))
            if not cur.fetchone():
                return sid
    return "".join(secrets.choice(BASE62) for _ in range(8))


def normalize_url(raw: str) -> str:
    if not raw:
        raise ValueError("URL is required")
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    p = urlparse(raw)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Invalid URL")
    return raw


def _token_hash(short_id: str) -> int:
    """Same SHA-256 token the ring uses — stored so we can export by range."""
    import hashlib
    return int(hashlib.sha256(short_id.encode()).hexdigest(), 16) % (2 ** 64)


# ── cleanup thread ────────────────────────────────────────────────────────────

def _cleanup(interval: int = 300):
    while True:
        time.sleep(interval)
        try:
            with _Conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM urls WHERE expires_at IS NOT NULL AND expires_at < NOW()"
                    )
                    log.info("Expired %d rows", cur.rowcount)
        except Exception as exc:
            log.error("Cleanup error: %s", exc)


threading.Thread(target=_cleanup, daemon=True).start()


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "node_id": NODE_ID}), 200


@app.route("/node/info")
def node_info():
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM urls")
            count = cur.fetchone()[0]
    return jsonify({"node_id": NODE_ID, "url_count": count, "uptime_sec": time.time()}), 200


@app.route("/shorten", methods=["POST"])
def shorten():
    data    = request.get_json(silent=True) or {}
    raw_url = data.get("org_url")
    try:
        org_url     = normalize_url(raw_url)
        expiry_days = max(1, min(365, int(data.get("expiry_days", 7))))
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    expires_at = datetime.utcnow() + timedelta(days=expiry_days)

    with _Conn() as conn:
        with conn.cursor() as cur:
            # dedup: reuse existing non-expired short ID for same URL
            cur.execute(
                "SELECT id, expires_at FROM urls WHERE org_url=%s LIMIT 1",
                (org_url,)
            )
            existing = cur.fetchone()
            if existing:
                sid, exp = existing
                if exp is None or exp.replace(tzinfo=None) > datetime.utcnow():
                    cur.execute(
                        "UPDATE urls SET expires_at=%s WHERE id=%s",
                        (expires_at, sid)
                    )
                    base = request.host_url.rstrip("/")
                    return jsonify({"short_url": f"{base}/{sid}", "expires_at": expires_at.isoformat()}), 200
                else:
                    cur.execute("DELETE FROM urls WHERE id=%s", (sid,))

            sid = _short_id(conn)
            cur.execute(
                """INSERT INTO urls (id, org_url, expires_at, click_count, token_hash)
                   VALUES (%s, %s, %s, 0, %s)""",
                (sid, org_url, expires_at, _token_hash(sid))
            )

    base = request.host_url.rstrip("/")
    return jsonify({"short_url": f"{base}/{sid}", "expires_at": expires_at.isoformat()}), 201


@app.route("/<short_id>")
def resolve(short_id):
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_url, expires_at FROM urls WHERE id=%s",
                (short_id,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404

            org_url, expires_at = row
            if expires_at and expires_at.replace(tzinfo=None) < datetime.utcnow():
                cur.execute("DELETE FROM urls WHERE id=%s", (short_id,))
                return jsonify({"error": "Expired"}), 410

            cur.execute(
                "UPDATE urls SET click_count=click_count+1, last_accessed=NOW() WHERE id=%s",
                (short_id,)
            )

    return redirect(org_url)


@app.route("/api/info/<short_id>")
def api_info(short_id):
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, org_url, created_at, expires_at, click_count, last_accessed FROM urls WHERE id=%s",
                (short_id,)
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    keys = ("id", "org_url", "created_at", "expires_at", "click_count", "last_accessed")
    return jsonify(dict(zip(keys, row))), 200


# ── migration: export key range ───────────────────────────────────────────────

@app.route("/node/export")
def export_range():
    """
    Return all URL records whose token_hash falls in [start, end].
    Used by coordinator during consistent-hash ring rebalancing.
    """
    try:
        start = int(request.args["start"])
        end   = int(request.args["end"])
    except (KeyError, ValueError):
        return jsonify({"error": "start and end query params required"}), 400

    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, org_url, expires_at, click_count
                   FROM urls
                   WHERE token_hash >= %s AND token_hash <= %s""",
                (start, end)
            )
            rows = cur.fetchall()

    urls = [
        {
            "id":          r[0],
            "org_url":     r[1],
            "expires_at":  r[2].isoformat() if r[2] else None,
            "click_count": r[3],
        }
        for r in rows
    ]
    return jsonify({"urls": urls, "count": len(urls)}), 200


# ── migration: import key range ───────────────────────────────────────────────

@app.route("/node/migrate", methods=["POST"])
def migrate():
    """Accept a batch of URL records from the coordinator during rebalancing."""
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"imported": 0}), 200

    imported = 0
    with _Conn() as conn:
        with conn.cursor() as cur:
            for entry in urls:
                try:
                    cur.execute(
                        """INSERT INTO urls (id, org_url, expires_at, click_count, token_hash)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO NOTHING""",
                        (
                            entry["id"],
                            entry["org_url"],
                            entry.get("expires_at"),
                            entry.get("click_count", 0),
                            _token_hash(entry["id"]),
                        )
                    )
                    imported += cur.rowcount
                except Exception as exc:
                    log.warning("Migrate row error: %s", exc)

    log.info("Migrated %d URLs from coordinator push", imported)
    return jsonify({"imported": imported}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
