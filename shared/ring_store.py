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
from shared import redis_health

log = logging.getLogger(__name__)

REDIS_URL   = os.getenv("REDIS_URL", "redis://redis:6379/0")
RING_KEY    = "shortly:ring:nodes"
VERSION_KEY = "shortly:ring:version"
MAINTENANCE_KEY = "shortly:ring:maintenance"
_shared_client = None


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.from_url(REDIS_URL, decode_responses=True,
                                        socket_connect_timeout=1, socket_timeout=2,
                                        health_check_interval=30)
    return _shared_client


def save_ring(nodes: list[dict], expected_version: int | None = None,
              lease_token: str | None = None) -> int:
    """
    Atomically write node list to Redis and increment version.
    Returns the new version number.
    """
    r = _client()
    with r.pipeline() as pipe:
        while True:
            try:
                pipe.watch(VERSION_KEY, MAINTENANCE_KEY)
                current = int(pipe.get(VERSION_KEY) or 0)
                if expected_version is not None and current != expected_version:
                    raise RingConflict(f"Ring changed from v{expected_version} to v{current}")
                if lease_token is not None and pipe.get(MAINTENANCE_KEY) != lease_token:
                    raise RingConflict("Ring maintenance lease was lost; refusing to flip")
                pipe.multi()
                pipe.set(RING_KEY, json.dumps(nodes))
                pipe.incr(VERSION_KEY)
                results = pipe.execute()
                version = int(results[1])
                break
            except redis.WatchError:
                if expected_version is not None:
                    raise RingConflict("Concurrent ring mutation")
    log.info("Ring saved to Redis (version=%d, nodes=%d)", version, len(nodes))
    return version


class RingConflict(Exception):
    """Optimistic ring version or mutation lease conflict."""


def acquire_maintenance(token: str) -> bool:
    """Allow one administrator at a time to pause shortens for migration."""
    return bool(_client().set(MAINTENANCE_KEY, token, nx=True, ex=600))


def release_maintenance(token: str) -> None:
    _client().eval("""if redis.call('GET',KEYS[1])==ARGV[1] then
                   return redis.call('DEL',KEYS[1]) else return 0 end""",
                   1, MAINTENANCE_KEY, token)


def refresh_maintenance(token: str) -> None:
    """Renew only an owned lease, and fail CLOSED if it expired/stole.

    An expired lease would reopen writes while data is in transit. No ring
    publish or pruning may take place after ownership is lost.
    """
    renewed = _client().eval("""if redis.call('GET',KEYS[1]) == ARGV[1]
                               then return redis.call('EXPIRE',KEYS[1],600)
                               else return 0 end""", 1, MAINTENANCE_KEY, token)
    if not renewed:
        raise RingConflict("Ring maintenance lease expired or changed")


def writes_paused() -> bool:
    return bool(_client().exists(MAINTENANCE_KEY))


def load_ring() -> tuple[list[dict], int]:
    """
    Returns (node_list, version).  node_list is [] and version is 0 if
    the key doesn't exist yet.
    """
    if redis_health.blocked():
        raise redis.ConnectionError("Redis circuit is open")
    try:
        # One Redis command observes ring + version at the same instant.
        # Separate GETs could read the OLD ring and NEW version around a
        # concurrent save, leaving a coordinator permanently misrouted.
        raw, version_raw = _client().mget(RING_KEY, VERSION_KEY)
        version = int(version_raw or 0)
        nodes = json.loads(raw) if raw else []
        redis_health.recovered()
    except redis.RedisError:
        redis_health.failed()
        raise
    log.info("Ring loaded from Redis (version=%d, nodes=%d)", version, len(nodes))
    return nodes, version


def current_version() -> int:
    if redis_health.blocked():
        raise redis.ConnectionError("Redis circuit is open")
    try:
        value = int(_client().get(VERSION_KEY) or 0)
        redis_health.recovered()
        return value
    except redis.RedisError:
        redis_health.failed()
        raise


def ping() -> bool:
    """Return True if Redis is reachable."""
    try:
        if redis_health.blocked():
            return False
        _client().ping()
        redis_health.recovered()
        return True
    except Exception:
        redis_health.failed()
        return False
