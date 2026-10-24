import os
import sys
import time
import logging
import threading
import string
import secrets
import uuid
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from urllib.parse import urlsplit
import requests as req_lib

from flask import Flask, request, jsonify, redirect, g, Response
from dataclasses import asdict
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.consistent_hash import ConsistentHashRing, NodeInfo
from shared.ring_store       import (save_ring, load_ring, current_version, ping as redis_ping,
                                     RingConflict, acquire_maintenance, release_maintenance,
                                     refresh_maintenance, writes_paused)
from shared.redis_cache      import (
    cache_get, cache_set, cache_invalidate, cache_clear,
    cache_snapshot, ping as cache_ping, NEGATIVE_SENTINEL,
)
from shared.auth    import require_api_key
from shared.auth import VALID_KEYS
from shared.migrate import migrate_ring, prune_moved, MigrationFailure
from shared.url_utils import normalize_url

# ── config ────────────────────────────────────────────────────────────────────

CACHE_CAPACITY   = int(os.getenv("CACHE_SIZE", "2000"))
VNODES           = int(os.getenv("VNODES_PER_NODE", "150"))
# Keep the complete node call strictly inside the 4-second failure budget,
# including coordinator serialization and HTTP error handling.
REQUEST_TIMEOUT  = (0.75, 2.75)
RETRY_ON_FAIL    = 0
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
BASE62 = string.ascii_letters + string.digits

logging.basicConfig(level=logging.INFO, format="%(asctime)s [coordinator] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
HTTP_REQUESTS = Counter("shortly_http_requests_total", "HTTP responses", ["route", "method", "status"])
HTTP_LATENCY = Histogram("shortly_http_request_seconds", "HTTP latency", ["route", "method"])
NODE_ERRORS = Counter("shortly_node_errors_total", "Failed backend calls", ["node"])
CACHE_EVENTS = Counter("shortly_cache_events_total", "Cache hits and misses", ["event"])
_failures = {}
_failure_lock = threading.Lock()
# DNS resolution is not covered by requests' connect/read timeout on every
# platform. Bound GET forwarding externally so a stopped Docker service
# cannot hold an HTTP request open for 4+ seconds resolving its hostname.
_node_read_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="shortly-node-get")


@app.before_request
def _before_request():
    g.started_at = time.monotonic()
    g.request_id = request.headers.get("X-Request-ID", "")[:80] or str(uuid.uuid4())


@app.after_request
def _after_request(response):
    route = request.url_rule.rule if request.url_rule else "unmatched"
    HTTP_REQUESTS.labels(route, request.method, str(response.status_code)).inc()
    elapsed = time.monotonic() - g.started_at
    HTTP_LATENCY.labels(route, request.method).observe(elapsed)
    response.headers["X-Request-ID"] = g.request_id
    log.info(json.dumps({"request_id": g.request_id, "route": route,
                         "method": request.method, "status": response.status_code,
                         "duration_ms": round(elapsed * 1000, 2)}))
    return response


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


def make_short_id() -> str:
    return "".join(secrets.choice(BASE62) for _ in range(7))

# ── ring: version-checked per-worker cache ────────────────────────────────────
# Each Gunicorn worker keeps a local ConsistentHashRing.
# Before every use we check the Redis version counter. If it changed,
# we reload from Redis. This gives us consistency across workers without
# a Redis round-trip on every single key lookup.

_local_ring = ConsistentHashRing(vnodes_per_node=VNODES)
_local_version = -1          # force reload on first call
_local_last_checked = 0.0
_ring_lock = threading.Lock()


def get_ring() -> ConsistentHashRing:
    """Return an up-to-date ring, reloading from Redis if version changed."""
    global _local_ring, _local_version, _local_last_checked
    if _local_version >= 0 and time.monotonic() - _local_last_checked < 1:
        return _local_ring
    try:
        redis_ver = current_version()
        _local_last_checked = time.monotonic()
    except Exception:
        return _local_ring   # Redis blip — use stale ring rather than crash

    if redis_ver == _local_version:
        return _local_ring

    with _ring_lock:
        try:
            # Redis can disappear between the initial version check and this
            # reload. Continue serving the last known ring in that case.
            if current_version() == _local_version:
                return _local_ring
            nodes, version = load_ring()
        except Exception:
            return _local_ring
        new_ring = ConsistentHashRing(vnodes_per_node=VNODES)
        for n in nodes:
            try:
                new_ring.add_node(NodeInfo(**n))
            except ValueError:
                pass
        _local_ring = new_ring
        _local_version = version
        log.info("Ring reloaded from Redis (v%d, %d nodes)", version, len(nodes))
    return _local_ring


def _persist_ring(ring: ConsistentHashRing, expected_version: int,
                  lease_token: str | None = None):
    """Write ring to Redis and invalidate all workers' caches."""
    global _local_version, _local_ring, _local_last_checked
    nodes = [asdict(n) for n in ring.get_all_nodes()]
    new_version = save_ring(nodes, expected_version=expected_version,
                            lease_token=lease_token)
    _local_version = new_version
    _local_ring = ring
    _local_last_checked = time.monotonic()


# ── helpers ───────────────────────────────────────────────────────────────────

def _forward(node: NodeInfo, method: str, path: str, retries: int = RETRY_ON_FAIL, **kwargs):
    url = f"{node.host}{path}"
    with _failure_lock:
        failures, opened_until = _failures.get(node.node_id, (0, 0))
        if failures >= 5 and time.monotonic() < opened_until:
            return None
    headers = dict(kwargs.pop("headers", {}))
    headers["X-Request-ID"] = g.get("request_id", str(uuid.uuid4()))
    try:
        if method.upper() == "GET":
            future = _node_read_executor.submit(
                req_lib.request, method, url, timeout=REQUEST_TIMEOUT,
                headers=headers, **kwargs)
            try:
                resp = future.result(timeout=3.5)
            except FutureTimeoutError:
                future.cancel()
                log.warning("Node %s GET exceeded 3.5s end-to-end budget", node.node_id)
                resp = None
        else:
            # Writes must complete synchronously: a timed-out asynchronous
            # POST might succeed AFTER the API responds with a 503.
            resp = req_lib.request(method, url, timeout=REQUEST_TIMEOUT,
                                   headers=headers, **kwargs)
        if resp is None:
            raise req_lib.exceptions.Timeout("Node read deadline exceeded")
        if resp.status_code < 500:
            with _failure_lock:
                _failures[node.node_id] = (0, 0)
            return resp
        log.warning("Node %s returned %s", node.node_id, resp.status_code)
    except req_lib.exceptions.RequestException as exc:
        log.warning("Node %s unavailable: %s", node.node_id, exc)
    NODE_ERRORS.labels(node.node_id).inc()
    with _failure_lock:
        failures, _ = _failures.get(node.node_id, (0, 0))
        failures += 1
        _failures[node.node_id] = (failures, time.monotonic() + 10 if failures >= 5 else 0)
    return None


def _ring_node_or_503(key: str):
    ring = get_ring()
    node = ring.get_node(key)
    if node is None:
        return None, (jsonify({"error": "No storage nodes registered"}), 503)
    return node, None


# ── health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    ring      = get_ring()
    ring_ok   = ring.stats().total_nodes > 0
    redis_ok  = redis_ping()
    # Health remains serving-ready with a previously loaded ring during a
    # cache/Redis outage. Mark degraded but do not remove otherwise healthy
    # coordinators from load-balancing and make all redirects unavailable.
    status = "ok" if redis_ok and ring_ok else "degraded"
    return jsonify({
        "status": status,
        "nodes":  ring.stats().total_nodes,
        "redis":  redis_ok,
    }), 200 if (redis_ok or ring_ok) else 503


# ── write path ────────────────────────────────────────────────────────────────

@app.route("/shorten", methods=["POST"])
def shorten():
    data    = request.get_json(silent=True) or {}
    try:
        own_hosts = {urlsplit(PUBLIC_BASE_URL).hostname, request.host.split(":", 1)[0]}
        org_url = normalize_url(data.get("org_url"), blocked_hosts=own_hosts)
        expiry_days = int(data.get("expiry_days", 7))
        if not 1 <= expiry_days <= 365:
            raise ValueError("expiry_days must be between 1 and 365")
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        if writes_paused():
            return jsonify({"error": "Ring maintenance in progress; retry shortly"}), 503
    except Exception:
        return jsonify({"error": "Ring state unavailable; cannot safely write"}), 503

    # The exact same ID selects the shard on both write and read. A node may
    # reject an ID collision, so try one additional independently random ID.
    for attempt in range(2):
        short_id = make_short_id()
        node, err = _ring_node_or_503(short_id)
        if err:
            return err
        resp = _forward(node, "POST", "/shorten",
                        json={"short_id": short_id, "org_url": org_url,
                              "expiry_days": expiry_days})
        if resp is None:
            return jsonify({"error": "Storage node unavailable"}), 503
        if resp.status_code == 409 and attempt == 0:
            continue
        if resp.status_code == 201:
            result = resp.json()
            if result.get("short_id") != short_id or result.get("org_url") != org_url:
                return jsonify({"error": "Storage node response mismatch"}), 502
            cache_set(short_id, org_url)
            result["short_url"] = f"{PUBLIC_BASE_URL or request.host_url.rstrip('/')}/{short_id}"
            log.info("Shortened -> node=%s id=%s", node.node_id, short_id)
            return jsonify(result), 201
        return resp.json(), resp.status_code
    return jsonify({"error": "Short ID collision; retry request"}), 409


# ── read path (cache-first) ───────────────────────────────────────────────────

@app.route("/<short_id>", methods=["GET"])
def resolve(short_id):
    cached = cache_get(short_id)
    if cached == NEGATIVE_SENTINEL:
        CACHE_EVENTS.labels("negative_hit").inc()
        return jsonify({"error": "Not found"}), 404
    if cached is not None:
        CACHE_EVENTS.labels("hit").inc()
        return redirect(cached)
    CACHE_EVENTS.labels("miss").inc()

    node, err = _ring_node_or_503(short_id)
    if err:
        return err

    resp = _forward(node, "GET", f"/{short_id}", allow_redirects=False)
    if resp is None:
        return jsonify({"error": "Storage node unavailable"}), 503

    if resp.status_code == 302:
        target = resp.headers.get("Location", "")
        if target:
            try:
                ttl = min(300, max(0, int(resp.headers.get("X-Shortly-TTL", "0"))))
            except ValueError:
                ttl = 0
            cache_set(short_id, target, ttl=ttl)
        return redirect(target)
    if resp.status_code == 404:
        cache_set(short_id, NEGATIVE_SENTINEL, ttl=30)
    return resp.content, resp.status_code, {"Content-Type": "application/json"}


# ── info passthrough ──────────────────────────────────────────────────────────

@app.route("/api/info/<short_id>")
def info(short_id):
    node, err = _ring_node_or_503(short_id)
    if err:
        return err
    resp = _forward(node, "GET", f"/api/info/{short_id}")
    if resp is None:
        return jsonify({"error": "Storage node unavailable"}), 503
    return resp.json(), resp.status_code


# ── admin: ring management ────────────────────────────────────────────────────

@app.route("/admin/nodes", methods=["GET"])
@require_api_key
def list_nodes():
    ring  = get_ring()
    stats = ring.stats()
    nodes = [asdict(n) for n in ring.get_all_nodes()]
    return jsonify({"nodes": nodes, "ring": asdict(stats)}), 200


@app.route("/admin/nodes", methods=["POST"])
@require_api_key
def add_node():
    data = request.get_json(silent=True) or {}
    for f in ("node_id", "host"):
        if not data.get(f):
            return jsonify({"error": f"{f} is required"}), 400

    try:
        new_node = NodeInfo(node_id=str(data["node_id"]),
                            host=data["host"].rstrip("/"),
                            weight=int(data.get("weight", 1)), vnodes=VNODES,
                            tags=data.get("tags", {}))
        if new_node.weight < 1 or new_node.weight > 10:
            raise ValueError("weight must be between 1 and 10")
        host = urlsplit(new_node.host)
        if (host.scheme not in {"http", "https"} or not host.hostname
                or host.username or host.password or host.query or host.fragment
                or host.path or not host.port):
            raise ValueError("host must be an http(s) origin with a port, without credentials or path")
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    return _change_membership(new_node=new_node)


def _change_membership(new_node=None, remove_id=None, force=False):
    # All coordinators observe this Redis lease. Writes are paused while data
    # moves, preventing a request from inserting on the old shard mid-copy.
    token = str(uuid.uuid4())
    try:
        if not acquire_maintenance(token):
            return jsonify({"error": "Concurrent membership change"}), 409
    except Exception:
        return jsonify({"error": "Redis unavailable; refusing ring change"}), 503
    try:
        nodes, version = load_ring()  # always load latest under lease
        ring = ConsistentHashRing(vnodes_per_node=VNODES)
        for n in nodes:
            ring.add_node(NodeInfo(**n))
        old_nodes = {n.node_id: n for n in ring.get_all_nodes()}
        if new_node is not None:
            # Without a preflight, an empty database or unlucky tiny corpus
            # could move zero records and let a dead/misnamed new node join.
            try:
                probe = req_lib.get(f"{new_node.host}/health", timeout=(0.75, 2.75))
                if (probe.status_code != 200 or
                        probe.json().get("node_id") != new_node.node_id):
                    return jsonify({"error": "New node failed health/identity preflight",
                                    "ring_unchanged": True}), 409
            except (req_lib.exceptions.RequestException, ValueError):
                return jsonify({"error": "New node unavailable or invalid",
                                "ring_unchanged": True}), 409
            ranges = ring.add_node(new_node)
        else:
            if len(old_nodes) <= 1 and not force:
                return jsonify({"error": "Refusing to remove last storage node"}), 409
            ranges = ring.remove_node(remove_id)

        # Drain in-flight shortens after setting the write barrier (node read
        # timeout 3s, connection timeout 1s). No new writes start meanwhile.
        if old_nodes:
            time.sleep(4.2)
        refresh_maintenance(token)
        api_key = next(iter(VALID_KEYS))
        warning = None
        if not ring.get_all_nodes() and force:
            summary, pruning = {"exported": 0, "moved": 0, "verified": 0}, {}
            warning = "Forced removal of the last node makes its links unavailable"
        else:
            try:
                summary, pruning = migrate_ring(
                    old_nodes, ring, api_key,
                    heartbeat=lambda: refresh_maintenance(token),
                    skip_unreachable_source_id=remove_id if (force and new_node is None) else None)
            except MigrationFailure as exc:
                return jsonify({"error": str(exc), "ring_unchanged": True}), 409
            if summary.get("unavailable_sources"):
                warning = ("Forced removal: records on unavailable "
                           f"{', '.join(summary['unavailable_sources'])} were not copied; "
                           "their short links may be permanently unavailable")
        refresh_maintenance(token)
        _persist_ring(ring, expected_version=version, lease_token=token)
        # Old copies are retained until every coordinator has refreshed its
        # local ring cache (1-second max refresh interval).
        # Give all coordinator caches (1s TTL) and in-flight bounded GETs
        # (3.5s deadline) time to stop using the former ring before pruning.
        time.sleep(4.2)
        cleanup_warnings = prune_moved(old_nodes, pruning, api_key) if redis_ping() else ["Redis unavailable: old copies intentionally retained"]
        result = {"status": "added" if new_node is not None else "removed",
                  "migration_ranges": len(ranges), "migration_summary": summary,
                  "ring": asdict(ring.stats()), "cleanup_warnings": cleanup_warnings}
        if warning:
            result["warning"] = warning
        return jsonify(result), 201 if new_node is not None else 200
    except RingConflict as exc:
        return jsonify({"error": str(exc), "ring_unchanged": True}), 409
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    finally:
        try:
            release_maintenance(token)
        except Exception:
            log.exception("Cannot release ring maintenance lease; it will expire")


@app.route("/admin/nodes/<node_id>", methods=["DELETE"])
@require_api_key
def remove_node(node_id):
    return _change_membership(remove_id=node_id,
                              force=request.args.get("force", "false").lower() == "true")


# ── admin: cache ──────────────────────────────────────────────────────────────

@app.route("/admin/cache")
@require_api_key
def get_cache_stats():
    from dataclasses import asdict as dc_asdict
    return jsonify(dc_asdict(cache_snapshot(CACHE_CAPACITY))), 200


@app.route("/admin/cache/invalidate/<short_id>", methods=["DELETE"])
@require_api_key
def invalidate(short_id):
    removed = cache_invalidate(short_id)
    return jsonify({"invalidated": removed}), 200


@app.route("/admin/cache/clear", methods=["DELETE"])
@require_api_key
def clear_cache():
    cache_clear()
    return jsonify({"status": "cleared"}), 200


# React/Vite dashboard is served by nginx; Flask has no duplicate HTML.

@app.route("/api/stats")
def api_stats():
    """Public, read-only snapshot of ring + cache state for the frontend dashboard."""
    ring_data = get_ring().stats()
    cache_data = cache_snapshot(CACHE_CAPACITY)
    from dataclasses import asdict as dc_asdict
    return jsonify({
        "redis_ok": redis_ping(),
        "ring": dc_asdict(ring_data),
        "cache": dc_asdict(cache_data),
    }), 200


@app.route("/")
def service_root():
    return jsonify({"service": "shortly-coordinator", "status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
