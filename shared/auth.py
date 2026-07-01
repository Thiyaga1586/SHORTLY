"""
shared/auth.py
--------------
Lightweight API-key guard for /admin/* endpoints.

Usage in Flask views:
    from shared.auth import require_api_key

    @app.route("/admin/nodes", methods=["GET"])
    @require_api_key
    def list_nodes(): ...

The key is read from the X-API-Key request header.
Expected key(s) come from the ADMIN_API_KEYS environment variable
(comma-separated list so you can rotate without downtime).

If ADMIN_API_KEYS is unset the guard is ACTIVE and will reject all
requests — this forces an explicit decision to set a key in production.
"""

import os
import hmac
import functools
import logging
from flask import request, jsonify

log = logging.getLogger(__name__)

_RAW = os.getenv("ADMIN_API_KEYS", "")
VALID_KEYS: set[str] = {k.strip() for k in _RAW.split(",") if k.strip()}

if not VALID_KEYS:
    log.warning(
        "ADMIN_API_KEYS is not set — all /admin requests will be rejected. "
        "Set it to a comma-separated list of secret tokens."
    )


def _constant_time_check(provided: str) -> bool:
    """Compare provided key against all valid keys in constant time."""
    for valid in VALID_KEYS:
        if hmac.compare_digest(provided.encode(), valid.encode()):
            return True
    return False


def require_api_key(f):
    """Decorator that enforces X-API-Key header on the wrapped route."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not VALID_KEYS:
            return jsonify({"error": "Admin API keys not configured on server"}), 503
        key = request.headers.get("X-API-Key", "")
        if not key or not _constant_time_check(key):
            log.warning("Rejected admin request from %s — bad API key", request.remote_addr)
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper
