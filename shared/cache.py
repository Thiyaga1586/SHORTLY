"""
cache.py
--------
Thread-safe LRU cache with TTL and rich hit-rate telemetry.

Why not just use functools.lru_cache?
  - We need per-entry TTL (expired URLs should fall through to DB)
  - We need live metrics (hit rate, eviction count) for benchmarking
  - We need an explicit invalidation API (on URL creation / deletion)

Implementation
  - doubly-linked list + dict for O(1) get/put  (classic LRU)
  - a monotone 64-bit logical clock avoids datetime.now() calls in hot path
  - snapshot() returns a copy so callers never hold the lock long
"""

import time
import threading
from dataclasses import dataclass
from typing import Any, Optional


# ── internal node ─────────────────────────────────────────────────────────────

class _Node:
    __slots__ = ("key", "value", "expires_at", "prev", "next")

    def __init__(self, key, value, expires_at: float):
        self.key = key
        self.value = value
        self.expires_at = expires_at
        self.prev: Optional["_Node"] = None
        self.next: Optional["_Node"] = None


# ── cache snapshot ────────────────────────────────────────────────────────────

@dataclass
class CacheSnapshot:
    capacity: int
    size: int
    hits: int
    misses: int
    evictions: int
    expirations: int
    hit_rate: float       # hits / (hits + misses), 0.0 if no requests yet
    fill_ratio: float     # size / capacity


# ── LRU cache ─────────────────────────────────────────────────────────────────

class LRUCache:
    """
    Fixed-capacity LRU cache with per-entry TTL.

    Parameters
    ----------
    capacity : int
        Maximum number of entries.  When full, the LRU entry is evicted.
    default_ttl : float
        Seconds a cache entry lives.  None = no expiry.

    Usage
    -----
    cache = LRUCache(capacity=1000, default_ttl=300)
    cache.set("abc123", "https://example.com")
    url = cache.get("abc123")   # -> "https://example.com" or None
    cache.invalidate("abc123")
    snap = cache.snapshot()
    print(snap.hit_rate)
    """

    def __init__(self, capacity: int = 1000, default_ttl: Optional[float] = 300):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._default_ttl = default_ttl
        self._map: dict[Any, _Node] = {}

        # sentinel head / tail (never evicted, never returned)
        self._head = _Node(None, None, float("inf"))
        self._tail = _Node(None, None, float("inf"))
        self._head.next = self._tail
        self._tail.prev = self._head

        self._lock = threading.Lock()

        # metrics
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0

    # ── doubly-linked-list helpers ────────────────────────────────────────────

    def _unlink(self, node: _Node):
        node.prev.next = node.next
        node.next.prev = node.prev

    def _prepend(self, node: _Node):
        """Insert node right after head (most-recently-used position)."""
        node.next = self._head.next
        node.prev = self._head
        self._head.next.prev = node
        self._head.next = node

    # ── public API ────────────────────────────────────────────────────────────

    def get(self, key: Any) -> Optional[Any]:
        with self._lock:
            node = self._map.get(key)
            if node is None:
                self._misses += 1
                return None

            if node.expires_at is not None and time.monotonic() > node.expires_at:
                # expired: remove and count as miss
                self._unlink(node)
                del self._map[key]
                self._expirations += 1
                self._misses += 1
                return None

            # move to MRU position
            self._unlink(node)
            self._prepend(node)
            self._hits += 1
            return node.value

    def set(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        effective_ttl = ttl if ttl is not None else self._default_ttl
        expires_at = (time.monotonic() + effective_ttl) if effective_ttl is not None else None

        with self._lock:
            if key in self._map:
                # update existing
                node = self._map[key]
                node.value = value
                node.expires_at = expires_at
                self._unlink(node)
                self._prepend(node)
                return

            # evict LRU if at capacity
            if len(self._map) >= self._capacity:
                lru = self._tail.prev
                if lru is not self._head:
                    self._unlink(lru)
                    del self._map[lru.key]
                    self._evictions += 1

            node = _Node(key, value, expires_at)
            self._map[key] = node
            self._prepend(node)

    def invalidate(self, key: Any) -> bool:
        """Remove a key.  Returns True if the key existed."""
        with self._lock:
            node = self._map.pop(key, None)
            if node is not None:
                self._unlink(node)
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._map.clear()
            self._head.next = self._tail
            self._tail.prev = self._head

    def reset_metrics(self) -> None:
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._expirations = 0

    def snapshot(self) -> CacheSnapshot:
        with self._lock:
            total = self._hits + self._misses
            return CacheSnapshot(
                capacity=self._capacity,
                size=len(self._map),
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
                expirations=self._expirations,
                hit_rate=round(self._hits / total, 4) if total else 0.0,
                fill_ratio=round(len(self._map) / self._capacity, 4),
            )

    def __len__(self):
        with self._lock:
            return len(self._map)

    def __repr__(self):
        s = self.snapshot()
        return f"<LRUCache cap={s.capacity} size={s.size} hit_rate={s.hit_rate:.2%}>"
