"""
shared/redis_cache.py
---------------------
Distributed read cache backed by Redis.  Replaces the in-process LRUCache
so all coordinator replicas share one cache pool.

Why Redis over in-process:
  - 3 coordinators behind nginx each had a separate LRU → effective hit rate
    divided by 3 (same key had to warm independently in each process).
  - Redis pipeline keeps get + metric-bump in one RTT.
  - Redis TTL handled server-side — no per-entry expiry thread needed.

Metrics are stored as Redis counters (shortly:cache:hits / misses / …)
so the /admin/cache endpoint reads the same counters regardless of which
coordinator replica handles the request.

CacheSnapshot dataclass mirrors the one in shared/cache.py so the dashboard
template needs no changes.
"""

import os
import logging
import threading
import time
from dataclasses import dataclass

import redis
from shared import redis_health

log = logging.getLogger(__name__)

REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL     = int(os.getenv("CACHE_TTL", "300"))          # seconds
CACHE_KEY_PFX = "shortly:url:"
HIT_KEY       = "shortly:cache:hits"
MISS_KEY      = "shortly:cache:misses"
EVICT_KEY     = "shortly:cache:evictions"   # Redis LRU evictions (approximate)
NEGATIVE_SENTINEL = "!shortly:not-found!"
_shared_client = None
_snapshot_lock = threading.Lock()
_last_snapshot = None
_snapshot_checked_at = 0.0


@dataclass
class CacheSnapshot:
    capacity: int
    size: int
    hits: int
    misses: int
    evictions: int
    expirations: int
    hit_rate: float
    fill_ratio: float


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.from_url(REDIS_URL, decode_responses=True,
                                        socket_connect_timeout=1, socket_timeout=2,
                                        health_check_interval=30)
    return _shared_client


def cache_get(short_id: str) -> str | None:
    """Return the cached URL or None.  Updates hit/miss counters."""
    if redis_health.blocked():
        return None
    try:
        r = _client()
        key = f"{CACHE_KEY_PFX}{short_id}"
        with r.pipeline(transaction=False) as pipe:
            pipe.get(key)
            value = pipe.execute()[0]
        r.incr(HIT_KEY if value is not None else MISS_KEY)
        redis_health.recovered()
        return value
    except redis.RedisError as exc:
        redis_health.failed()
        log.warning("Cache unavailable, falling through to storage: %s", exc)
        return None


def cache_set(short_id: str, url: str, ttl: int = CACHE_TTL) -> None:
    if ttl <= 0 or redis_health.blocked():
        return
    try:
        _client().setex(f"{CACHE_KEY_PFX}{short_id}", min(ttl, CACHE_TTL), url)
    except redis.RedisError as exc:
        redis_health.failed()
        log.warning("Cache write skipped: %s", exc)


def cache_invalidate(short_id: str) -> bool:
    if redis_health.blocked():
        return False
    try:
        return bool(_client().delete(f"{CACHE_KEY_PFX}{short_id}"))
    except redis.RedisError:
        redis_health.failed()
        return False


def cache_clear() -> None:
    global _snapshot_checked_at
    r = _client()
    keys = []
    for key in r.scan_iter(match=f"{CACHE_KEY_PFX}*", count=200):
        keys.append(key)
        if len(keys) == 200:
            r.delete(*keys)
            keys.clear()
    if keys:
        r.delete(*keys)
    r.set(HIT_KEY, 0)
    r.set(MISS_KEY, 0)
    r.set(EVICT_KEY, 0)
    with _snapshot_lock:
        _snapshot_checked_at = 0.0


def cache_snapshot(capacity: int) -> CacheSnapshot:
    """At most one SCAN per process/5 s; remain usable during Redis outage."""
    global _last_snapshot, _snapshot_checked_at
    with _snapshot_lock:
        if (_last_snapshot is not None and
                time.monotonic() - _snapshot_checked_at < 5):
            return _last_snapshot
        if redis_health.blocked():
            return _last_snapshot or CacheSnapshot(capacity, 0, 0, 0, 0, 0, 0.0, 0.0)
        try:
            r = _client()
            with r.pipeline(transaction=False) as pipe:
                pipe.get(HIT_KEY)
                pipe.get(MISS_KEY)
                pipe.get(EVICT_KEY)
                values = pipe.execute()
            hits, misses, evictions = [int(v or 0) for v in values]
            size = sum(1 for _ in r.scan_iter(match=f"{CACHE_KEY_PFX}*", count=200))
            total = hits + misses
            _last_snapshot = CacheSnapshot(
                capacity=capacity, size=size, hits=hits, misses=misses,
                evictions=evictions, expirations=0,
                hit_rate=round(hits / total, 4) if total else 0.0,
                fill_ratio=round(size / capacity, 4) if capacity else 0.0)
            _snapshot_checked_at = time.monotonic()
            redis_health.recovered()
        except redis.RedisError as exc:
            redis_health.failed()
            log.warning("Cache statistics unavailable: %s", exc)
        return _last_snapshot or CacheSnapshot(capacity, 0, 0, 0, 0, 0, 0.0, 0.0)


def ping() -> bool:
    try:
        _client().ping()
        return True
    except Exception:
        return False
