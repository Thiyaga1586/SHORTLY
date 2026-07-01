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
from dataclasses import dataclass

import redis

log = logging.getLogger(__name__)

REDIS_URL     = os.getenv("REDIS_URL", "redis://redis:6379/0")
CACHE_TTL     = int(os.getenv("CACHE_TTL", "300"))          # seconds
CACHE_KEY_PFX = "shortly:url:"
HIT_KEY       = "shortly:cache:hits"
MISS_KEY      = "shortly:cache:misses"
EVICT_KEY     = "shortly:cache:evictions"   # Redis LRU evictions (approximate)


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
    return redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=2)


def cache_get(short_id: str) -> str | None:
    """Return the cached URL or None.  Updates hit/miss counters."""
    r = _client()
    key = f"{CACHE_KEY_PFX}{short_id}"
    val = r.get(key)
    if val:
        r.incr(HIT_KEY)
        return val
    r.incr(MISS_KEY)
    return None


def cache_set(short_id: str, url: str, ttl: int = CACHE_TTL) -> None:
    r = _client()
    r.setex(f"{CACHE_KEY_PFX}{short_id}", ttl, url)


def cache_invalidate(short_id: str) -> bool:
    r = _client()
    deleted = r.delete(f"{CACHE_KEY_PFX}{short_id}")
    return bool(deleted)


def cache_clear() -> None:
    r = _client()
    keys = r.keys(f"{CACHE_KEY_PFX}*")
    if keys:
        r.delete(*keys)
    r.set(HIT_KEY, 0)
    r.set(MISS_KEY, 0)
    r.set(EVICT_KEY, 0)


def cache_snapshot(capacity: int) -> CacheSnapshot:
    r = _client()
    hits      = int(r.get(HIT_KEY) or 0)
    misses    = int(r.get(MISS_KEY) or 0)
    evictions = int(r.get(EVICT_KEY) or 0)
    size      = len(r.keys(f"{CACHE_KEY_PFX}*"))
    total     = hits + misses
    return CacheSnapshot(
        capacity=capacity,
        size=size,
        hits=hits,
        misses=misses,
        evictions=evictions,
        expirations=0,           # Redis TTL handles this transparently
        hit_rate=round(hits / total, 4) if total else 0.0,
        fill_ratio=round(size / capacity, 4) if capacity else 0.0,
    )


def ping() -> bool:
    try:
        _client().ping()
        return True
    except Exception:
        return False
