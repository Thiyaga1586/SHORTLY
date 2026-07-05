import os
import sys
import time
import logging
import threading
import requests as req_lib

from flask import Flask, request, jsonify, redirect, render_template_string
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.consistent_hash import ConsistentHashRing, NodeInfo
from shared.ring_store       import save_ring, load_ring, current_version, ping as redis_ping
from shared.redis_cache      import (
    cache_get, cache_set, cache_invalidate, cache_clear,
    cache_snapshot, ping as cache_ping,
)
from shared.auth    import require_api_key
from shared.migrate import migrate_ranges

# ── config ────────────────────────────────────────────────────────────────────

CACHE_CAPACITY   = int(os.getenv("CACHE_SIZE", "2000"))
VNODES           = int(os.getenv("VNODES_PER_NODE", "150"))
REQUEST_TIMEOUT  = float(os.getenv("NODE_TIMEOUT_SEC", "5"))
RETRY_ON_FAIL    = int(os.getenv("NODE_RETRIES", "1"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [coordinator] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── ring: version-checked per-worker cache ────────────────────────────────────
# Each Gunicorn worker keeps a local ConsistentHashRing.
# Before every use we check the Redis version counter. If it changed,
# we reload from Redis. This gives us consistency across workers without
# a Redis round-trip on every single key lookup.

_local_ring = ConsistentHashRing(vnodes_per_node=VNODES)
_local_version = -1          # force reload on first call
_ring_lock = threading.Lock()


def get_ring() -> ConsistentHashRing:
    """Return an up-to-date ring, reloading from Redis if version changed."""
    global _local_ring, _local_version
    try:
        redis_ver = current_version()
    except Exception:
        return _local_ring   # Redis blip — use stale ring rather than crash

    if redis_ver == _local_version:
        return _local_ring

    with _ring_lock:
        # Double-check inside lock
        if current_version() == _local_version:
            return _local_ring
        nodes, version = load_ring()
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


def _persist_ring(ring: ConsistentHashRing):
    """Write ring to Redis and invalidate all workers' caches."""
    global _local_version
    nodes = [asdict(n) for n in ring.get_all_nodes()]
    new_version = save_ring(nodes)
    _local_version = new_version   # update this worker immediately


# ── helpers ───────────────────────────────────────────────────────────────────

def _forward(node: NodeInfo, method: str, path: str, retries: int = RETRY_ON_FAIL, **kwargs):
    url = f"{node.host}{path}"
    for attempt in range(retries + 1):
        try:
            resp = req_lib.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code < 500:
                return resp
            log.warning("Node %s returned %d (attempt %d)", node.node_id, resp.status_code, attempt + 1)
        except req_lib.exceptions.RequestException as exc:
            log.error("Node %s unreachable (attempt %d): %s", node.node_id, attempt + 1, exc)
        if attempt < retries:
            time.sleep(0.2 * (attempt + 1))
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
    # Degraded only if Redis itself is down — ring can be empty on fresh start
    status = "ok" if redis_ok else "degraded"
    return jsonify({
        "status": status,
        "nodes":  ring.stats().total_nodes,
        "redis":  redis_ok,
    }), 200 if status == "ok" else 503


# ── write path ────────────────────────────────────────────────────────────────

@app.route("/shorten", methods=["POST"])
def shorten():
    data    = request.get_json(silent=True) or {}
    org_url = data.get("org_url", "")
    if not org_url:
        return jsonify({"error": "org_url is required"}), 400

    node, err = _ring_node_or_503(org_url)
    if err:
        return err

    resp = _forward(node, "POST", "/shorten", json=data)
    if resp is None:
        return jsonify({"error": "Storage node unavailable"}), 503

    if resp.status_code in (200, 201):
        result   = resp.json()
        short_id = result.get("short_url", "").rsplit("/", 1)[-1]
        if short_id:
            cache_set(short_id, result.get("org_url") or org_url)
            # Rewrite short_url to use the public-facing host (the one the
            # client actually connected to), not the internal node hostname
            # (e.g. http://node-3:5003) which is unreachable outside Docker.
            public_base = request.host_url.rstrip("/")
            result["short_url"] = f"{public_base}/{short_id}"
        log.info("Shortened -> node=%s id=%s", node.node_id, short_id)
        return jsonify(result), resp.status_code

    return resp.json(), resp.status_code


# ── read path (cache-first) ───────────────────────────────────────────────────

@app.route("/<short_id>", methods=["GET"])
def resolve(short_id):
    cached = cache_get(short_id)
    if cached:
        return redirect(cached)

    node, err = _ring_node_or_503(short_id)
    if err:
        return err

    resp = _forward(node, "GET", f"/{short_id}", allow_redirects=False)
    if resp is None:
        return jsonify({"error": "Storage node unavailable"}), 503

    if resp.status_code == 302:
        target = resp.headers.get("Location", "")
        if target:
            cache_set(short_id, target)
        return redirect(target)

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

    new_node = NodeInfo(
        node_id=data["node_id"],
        host=data["host"].rstrip("/"),
        weight=int(data.get("weight", 1)),
        vnodes=VNODES,
        tags=data.get("tags", {}),
    )

    # Work on a fresh copy of the ring to avoid race
    ring = get_ring()
    old_nodes = {n.node_id: n for n in ring.get_all_nodes()}

    try:
        migration_ranges = ring.add_node(new_node)
        _persist_ring(ring)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    summary = {"imported": 0, "errors": []}
    if old_nodes and migration_ranges:
        summary = migrate_ranges(migration_ranges, new_node, ring, old_nodes)

    return jsonify({
        "status":            "added",
        "migration_ranges":  len(migration_ranges),
        "migration_summary": summary,
        "ring":              asdict(ring.stats()),
    }), 201


@app.route("/admin/nodes/<node_id>", methods=["DELETE"])
@require_api_key
def remove_node(node_id):
    ring = get_ring()
    try:
        freed = ring.remove_node(node_id)
        _persist_ring(ring)
        return jsonify({
            "status":       "removed",
            "freed_ranges": len(freed),
            "ring":         asdict(ring.stats()),
        }), 200
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


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


# ── dashboard ─────────────────────────────────────────────────────────────────

DASHBOARD_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"/>
<title>Shortly — Dashboard</title>
<meta http-equiv="refresh" content="15">
<style>
body{font-family:monospace;max-width:1000px;margin:40px auto;background:#0f0f0f;color:#e0e0e0;padding:0 16px}
h1{color:#7cf;margin-bottom:4px}h2{color:#aef;border-bottom:1px solid #333;padding-bottom:4px}
table{width:100%;border-collapse:collapse;margin-bottom:24px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #222}
th{background:#1a1a2e;color:#7cf}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.8em}
.ok{background:#1a4a1a;color:#6f6}.warn{background:#4a3a00;color:#fc0}.err{background:#4a1a1a;color:#f66}
.section{background:#151515;padding:16px;border-radius:8px;margin-bottom:20px}
a{color:#7cf}
.tagline{color:#999;font-size:.95em;margin:6px 0 18px 0;line-height:1.5}
.stack{color:#6ad;font-size:.85em}
button{cursor:pointer;background:#2a4a8a;color:#fff;border:none;border-radius:4px}
button:hover{background:#345aa8}
input{background:#1a1a1a;border:1px solid #333;color:#e0e0e0;border-radius:4px}
.copybtn{padding:6px 10px;font-size:.85em;margin-left:8px}
</style></head><body>
<h1>⚡ Shortly — Distributed URL Shortener</h1>
<p class="tagline">
  A horizontally-scaled URL shortener using consistent hashing across independent Flask nodes,
  backed by PostgreSQL and a shared Redis cache.<br/>
  <span class="stack">Flask · Gunicorn · PostgreSQL · Redis · Docker Compose · Cloudflare Tunnel</span>
</p>
<p style="color:#888">
  Auto-refreshes every 15s &nbsp;|&nbsp;
  Redis: <span class="badge {{ 'ok' if redis_ok else 'err' }}">{{ 'connected' if redis_ok else 'DOWN' }}</span>
  &nbsp;|&nbsp; Nodes: {{ ring.total_nodes }}
</p>

<h2>Ring</h2>
<div class="section">
  <b>Nodes:</b> {{ ring.total_nodes }} &nbsp;
  <b>Virtual nodes:</b> {{ ring.total_vnodes }} &nbsp;
  <b>Load std-dev:</b> {{ ring.load_std_dev }}
  <table style="margin-top:12px">
    <tr><th>Node ID</th><th>V-nodes</th><th>Load share</th><th>Bar</th></tr>
    {% for nid, cnt in ring.vnode_distribution.items() %}
    {% set pct = (100 * cnt / ring.total_vnodes)|round(1) if ring.total_vnodes else 0 %}
    <tr><td>{{ nid }}</td><td>{{ cnt }}</td><td>{{ pct }}%</td>
      <td><div style="background:#2a4a8a;height:12px;width:{{ [pct|int * 2, 200]|min }}px"></div></td>
    </tr>{% endfor %}
  </table>
</div>

<h2>Cache (shared Redis)</h2>
<div class="section">
  <b>Capacity:</b> {{ cache.capacity }} &nbsp;
  <b>Live entries:</b> {{ cache.size }} &nbsp;
  <span class="badge {{ 'ok' if cache.hit_rate > 0.5 else 'warn' }}"
        title="Low hit rate is expected with light/first-time demo traffic — each unique URL is only requested once or twice, so there's little to cache yet.">
    Hit rate: {{ (cache.hit_rate * 100)|round(1) }}%
  </span>
  <table style="margin-top:12px">
    <tr><th>Hits</th><th>Misses</th><th>Evictions</th></tr>
    <tr><td>{{ cache.hits }}</td><td>{{ cache.misses }}</td><td>{{ cache.evictions }}</td></tr>
  </table>
</div>

<h2>Shorten a URL</h2>
<div class="section">
  <input id="url" placeholder="https://example.com" style="width:55%;padding:8px"/>
  <input id="days" type="number" value="7" style="width:60px;padding:8px"/>
  <button onclick="doShorten()" style="padding:8px 14px">Shorten</button>
  <button onclick="tryExample()" style="padding:8px 14px;background:#333" title="Fill in a sample URL and shorten it">Try an example</button>
  <div id="out" style="margin-top:12px;color:#6f6"></div>
</div>
<script>
async function shortenUrl(url, days) {
  const res = await fetch("/shorten", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({org_url: url, expiry_days: days})
  });
  const d = await res.json();
  const out = document.getElementById("out");
  if (res.ok) {
    out.innerHTML = `<b>Short URL:</b> <a href="${d.short_url}" target="_blank">${d.short_url}</a>` +
      `<button class="copybtn" onclick="copyLink('${d.short_url}')">Copy</button>`;
  } else {
    out.innerHTML = `<span style="color:#f66">${d.error}</span>`;
  }
}
function doShorten() {
  shortenUrl(document.getElementById("url").value,
             parseInt(document.getElementById("days").value));
}
function tryExample() {
  document.getElementById("url").value = "https://github.com/";
  shortenUrl("https://github.com/", 7);
}
function copyLink(link) {
  navigator.clipboard.writeText(link);
  const out = document.getElementById("out");
  const original = out.innerHTML;
  out.innerHTML += ` <span style="color:#aef">Copied!</span>`;
  setTimeout(() => { out.innerHTML = original; }, 1500);
}
</script>
</body></html>
"""

@app.route("/")
def dashboard():
    ring       = get_ring()
    ring_data  = ring.stats()
    cache_data = cache_snapshot(CACHE_CAPACITY)
    return render_template_string(
        DASHBOARD_HTML,
        ring=ring_data,
        cache=cache_data,
        redis_ok=redis_ping(),
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)