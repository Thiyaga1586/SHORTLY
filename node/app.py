"""
node/app.py  (production v2)
-----------------------------
Each storage node uses its own PostgreSQL database and small connection pool.
Schema startup and expiry cleanup run in dedicated per-container processes.
For correct per-container Prometheus counters, Gunicorn uses one threaded worker.
"""

import os
import sys
import threading
import time
import logging
import uuid
import json

import psycopg2
import psycopg2.pool
from flask import Flask, request, jsonify, redirect, g, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from shared.consistent_hash import ConsistentHashRing
from shared.url_utils import normalize_url
from shared.auth import require_api_key

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [" + os.getenv("NODE_ID", "node") + "] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
REQUESTS = Counter("shortly_node_requests_total", "Node responses", ["route", "method", "status"])
LATENCY = Histogram("shortly_node_request_seconds", "Node response latency", ["route", "method"])


@app.before_request
def _before_request():
    g.started_at = time.monotonic()
    g.request_id = request.headers.get("X-Request-ID", "")[:80] or str(uuid.uuid4())


@app.after_request
def _after_request(resp):
    path = request.url_rule.rule if request.url_rule else "unmatched"
    elapsed = time.monotonic() - g.started_at
    REQUESTS.labels(path, request.method, str(resp.status_code)).inc()
    LATENCY.labels(path, request.method).observe(elapsed)
    resp.headers["X-Request-ID"] = g.request_id
    log.info(json.dumps({"request_id": g.request_id, "route": path,
                         "method": request.method, "status": resp.status_code,
                         "duration_ms": round(elapsed * 1000, 2)}))
    return resp


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)

# ── config ────────────────────────────────────────────────────────────────────

NODE_ID  = os.getenv("NODE_ID", "node-0")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "",
)

# ── connection pool ───────────────────────────────────────────────────────────
# psycopg2 ThreadedConnectionPool is safe across Gunicorn threads.
# min=2, max=3 — keep below PostgreSQL's global connection limit.

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                min_conn = int(os.getenv("DB_POOL_MIN", "2"))
                max_conn = int(os.getenv("DB_POOL_MAX", "3"))
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
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        finally:
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
                    token_hash    NUMERIC(20,0) -- unsigned 64-bit SHA token
                );
                ALTER TABLE urls ALTER COLUMN token_hash TYPE NUMERIC(20,0);
                DROP INDEX IF EXISTS idx_org_url;
                CREATE INDEX IF NOT EXISTS idx_expires_at ON urls(expires_at);
                CREATE INDEX IF NOT EXISTS idx_token_hash ON urls(token_hash);
            """)
    log.info("DB schema ready: %s", DATABASE_URL.rsplit("@", 1)[-1])


# ── helpers ───────────────────────────────────────────────────────────────────

def _token_hash(short_id: str) -> int:
    """Exact unsigned token used by ConsistentHashRing (NUMERIC column)."""
    return ConsistentHashRing._hash(short_id)


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


# Cleanup runs in a dedicated singleton process, not every Gunicorn worker.


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        with _Conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return jsonify({"status": "ok", "node_id": NODE_ID, "database": "ok"}), 200
    except Exception:
        log.exception("Storage node health probe failed")
        return jsonify({"status": "degraded", "node_id": NODE_ID,
                        "database": "unavailable"}), 503


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
        sid = data["short_id"]
        if not isinstance(sid, str) or len(sid) != 7 or not sid.isascii() or not sid.isalnum():
            raise ValueError("short_id must contain 7 ASCII alphanumeric characters")
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except KeyError:
        return jsonify({"error": "short_id is required"}), 400

    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO urls (id, org_url, expires_at, click_count, token_hash)
                   VALUES (%s, %s, NOW() + make_interval(days => %s), 0, %s)
                   ON CONFLICT (id) DO NOTHING RETURNING expires_at""",
                (sid, org_url, expiry_days, _token_hash(sid)),
            )
            row = cur.fetchone()
    if row is None:
        return jsonify({"error": "Short ID collision"}), 409
    return jsonify({"short_id": sid, "org_url": org_url, "expires_at": row[0].isoformat()}), 201


@app.route("/<short_id>")
def resolve(short_id):
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_url, expires_at, GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (expires_at - NOW())))) FROM urls WHERE id=%s",
                (short_id,)
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Not found"}), 404

            org_url, expires_at, ttl = row
            if expires_at is not None and ttl <= 0:
                return jsonify({"error": "Expired"}), 410

            cur.execute(
                "UPDATE urls SET click_count=click_count+1, last_accessed=NOW() WHERE id=%s",
                (short_id,)
            )

    response = redirect(org_url)
    response.headers["X-Shortly-TTL"] = str(int(ttl)) if expires_at is not None else "300"
    return response


@app.route("/api/info/<short_id>")
def api_info(short_id):
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, org_url, created_at, expires_at, last_accessed FROM urls WHERE id=%s",
                (short_id,)
            )
            row = cur.fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    keys = ("id", "org_url", "created_at", "expires_at", "last_accessed")
    return jsonify(dict(zip(keys, row))), 200


# ── migration: export key range ───────────────────────────────────────────────

@app.route("/node/export")
@require_api_key
def export_range():
    """
    Return all URL records whose token_hash falls in [start, end].
    Used by coordinator during consistent-hash ring rebalancing.
    """
    try:
        full_export = request.args.get("all") == "1"
        start = int(request.args["start"]) if not full_export else 0
        end   = int(request.args["end"]) if not full_export else 2**64 - 1
    except (KeyError, ValueError):
        return jsonify({"error": "start and end query params required"}), 400
    if not (0 <= start < 2**64 and 0 <= end < 2**64):
        return jsonify({"error": "Range must be in unsigned 64-bit space"}), 400

    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, org_url, expires_at, click_count FROM urls
                WHERE (expires_at IS NULL OR expires_at > NOW())
                  AND (%s OR (%s < %s AND token_hash > %s AND token_hash <= %s)
                    OR (%s >= %s AND (token_hash > %s OR token_hash <= %s)))""",
                        (full_export, start, end, start, end, start, end, start, end))
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
@require_api_key
def migrate():
    """Accept a batch of URL records from the coordinator during rebalancing."""
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"imported": 0}), 200

    imported = 0
    verified = 0
    with _Conn() as conn:
        with conn.cursor() as cur:
            for entry in urls:
                try:
                    cur.execute(
                        """INSERT INTO urls (id, org_url, expires_at, click_count, token_hash)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (id) DO UPDATE SET
                              click_count = GREATEST(urls.click_count, EXCLUDED.click_count)
                           WHERE urls.org_url = EXCLUDED.org_url
                             AND urls.expires_at IS NOT DISTINCT FROM EXCLUDED.expires_at""",
                        (
                            entry["id"],
                            entry["org_url"],
                            entry.get("expires_at"),
                            entry.get("click_count", 0),
                            _token_hash(entry["id"]),
                        )
                    )
                    imported += cur.rowcount
                    cur.execute("SELECT org_url, expires_at, click_count FROM urls WHERE id=%s", (entry["id"],))
                    existing = cur.fetchone()
                    from datetime import datetime, timezone
                    expected_expiry = datetime.fromisoformat(entry["expires_at"]) if entry.get("expires_at") else None
                    if expected_expiry is not None and expected_expiry.tzinfo is None:
                        expected_expiry = expected_expiry.replace(tzinfo=timezone.utc)
                    if (existing and existing[0] == entry["org_url"]
                            and existing[1] == expected_expiry
                            and existing[2] >= entry.get("click_count", 0)):
                        verified += 1
                except Exception as exc:
                    log.warning("Migrate row error: %s", exc)
                    raise

    log.info("Migrated %d URLs from coordinator push", imported)
    return jsonify({"imported": imported, "verified": verified}), 200


@app.route("/node/prune", methods=["POST"])
@require_api_key
def prune():
    """Delete verified moved records after ring has switched (internal only)."""
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(i, str) for i in ids):
        return jsonify({"error": "Invalid ids"}), 400
    with _Conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM urls WHERE id = ANY(%s)", (ids,))
            deleted = cur.rowcount
    return jsonify({"deleted": deleted}), 200


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_cleanup, daemon=True).start()
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
