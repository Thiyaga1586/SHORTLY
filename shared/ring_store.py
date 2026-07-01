"""
shared/ring_store.py
--------------------
Persists the consistent-hash ring into Redis so every coordinator
replica sees the same node list and ring state survives restarts.

All coordinators call load_ring() on startup and push to
save_ring() after every mutation (add_node / remove_node).

Key layout in Redis:
  shortly:ring:nodes          -> JSON list of NodeInfo dicts
  shortly:ring:version        -> integer (monotonic, for optimistic concurrency)
"""

import json
import logging
import os
from typing import Optional

import redis

log = logging.getLogger(__name__)

REDIS_URL   = os.getenv("REDIS_URL", "redis://redis:6379/0")
RING_KEY    = "shortly:ring:nodes"
VERSION_KEY = "shortly:ring:version"


def _client() -> redis.Redis:
    return redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=3)


def save_ring(nodes: list[dict]) -> int:
    """
    Atomically write node list to Redis and increment version.
    Returns the new version number.
    """
    r = _client()
    pipe = r.pipeline(transaction=True)
    pipe.set(RING_KEY, json.dumps(nodes))
    pipe.incr(VERSION_KEY)
    results = pipe.execute()
    version = int(results[1])
    log.info("Ring saved to Redis (version=%d, nodes=%d)", version, len(nodes))
    return version


def load_ring() -> tuple[list[dict], int]:
    """
    Returns (node_list, version).  node_list is [] and version is 0 if
    the key doesn't exist yet.
    """
    r = _client()
    raw     = r.get(RING_KEY)
    version = int(r.get(VERSION_KEY) or 0)
    nodes   = json.loads(raw) if raw else []
    log.info("Ring loaded from Redis (version=%d, nodes=%d)", version, len(nodes))
    return nodes, version


def current_version() -> int:
    r = _client()
    return int(r.get(VERSION_KEY) or 0)


def ping() -> bool:
    """Return True if Redis is reachable."""
    try:
        _client().ping()
        return True
    except Exception:
        return False
