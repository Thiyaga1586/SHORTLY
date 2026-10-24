"""Shared, process-local Redis failure circuit for cache and ring lookups.

Docker DNS can be slow when a stopped container's service record disappears.
Once a connection fails, avoid repeating DNS attempts on every step in a
single redirect request. The circuit probes again after a short cooldown.
"""
import threading
import time

_lock = threading.Lock()
_blocked_until = 0.0


def blocked() -> bool:
    with _lock:
        return time.monotonic() < _blocked_until


def failed() -> None:
    global _blocked_until
    with _lock:
        _blocked_until = time.monotonic() + 5


def recovered() -> None:
    global _blocked_until
    with _lock:
        _blocked_until = 0.0
