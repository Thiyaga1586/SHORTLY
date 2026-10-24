"""Single URL normalization and validation path used before sharding/insertion."""
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048


def normalize_url(raw: str, blocked_hosts=()) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("URL is required")
    raw = raw.strip()
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise ValueError("URL contains control characters")
    if "://" not in raw:
        raw = "https://" + raw
    if len(raw) > MAX_URL_LENGTH:
        raise ValueError("URL exceeds 2048 characters")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        parsed.port  # validate malformed ports
    except ValueError as exc:
        raise ValueError("Invalid URL") from exc
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("Only http:// and https:// URLs are allowed")
    if parsed.username or parsed.password or " " in parsed.netloc:
        raise ValueError("Host may not contain credentials or spaces")
    if host.lower().rstrip(".") in {str(h).lower().rstrip(".") for h in blocked_hosts if h}:
        raise ValueError("Cannot shorten this service's own host")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))
