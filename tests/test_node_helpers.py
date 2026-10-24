from __future__ import annotations

from datetime import datetime, timezone

import pytest

from node import app as node_app
from shared.consistent_hash import ConsistentHashRing
from shared.url_utils import MAX_URL_LENGTH, normalize_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "https://example.com"),
        ("  example.com/path?q=1  ", "https://example.com/path?q=1"),
        ("http://example.com/a", "http://example.com/a"),
        ("https://example.com/a#frag", "https://example.com/a#frag"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "http://",
        "https:///missing-host",
        "ftp://example.com/file",
        "https://user:password@example.com",
        "https://bad host.example/path",
        "https://example.com:99999/path",
        "https://example.com/line\nbreak",
    ],
)
def test_normalize_url_rejects_invalid_values(raw) -> None:
    with pytest.raises(ValueError):
        normalize_url(raw)


def test_normalize_url_rejects_oversized_urls() -> None:
    raw = "https://example.com/" + "a" * MAX_URL_LENGTH
    with pytest.raises(ValueError, match="2048"):
        normalize_url(raw)


def test_normalize_url_can_block_the_public_service_host() -> None:
    with pytest.raises(ValueError, match="own host"):
        normalize_url("https://short.ly/path", blocked_hosts=("SHORT.LY.",))


def test_token_hash_matches_ring_unsigned_64_bit_domain() -> None:
    ids = [f"short-{i}" for i in range(100)]
    hashes = [node_app._token_hash(short_id) for short_id in ids]

    assert hashes == [ConsistentHashRing._hash(short_id) for short_id in ids]
    assert all(0 <= token < 2**64 for token in hashes)
    # PostgreSQL storage must support values in the unsigned half of the ring.
    assert any(token > 2**63 - 1 for token in hashes)


@pytest.mark.parametrize("short_id", ["short", "toolong88", "abc-123", "ébc1234"])
def test_storage_node_rejects_invalid_coordinator_short_ids_before_db_access(short_id: str) -> None:
    response = node_app.app.test_client().post(
        "/shorten",
        json={"org_url": "https://example.com", "expiry_days": 1, "short_id": short_id},
    )
    assert response.status_code == 400
    assert "short_id" in response.get_json()["error"]


def test_storage_health_is_503_when_database_is_unavailable(monkeypatch) -> None:
    def unavailable_pool():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(node_app, "_get_pool", unavailable_pool)

    response = node_app.app.test_client().get("/health")

    assert response.status_code == 503
    assert response.get_json() == {
        "status": "degraded",
        "node_id": node_app.NODE_ID,
        "database": "unavailable",
    }


def test_migration_duplicate_preserves_monotonic_click_count_and_verifies(monkeypatch) -> None:
    expiry = datetime(2026, 10, 25, tzinfo=timezone.utc)
    state = {
        "id": "Ab3dE9x",
        "org_url": "https://example.com/migrated",
        "expires_at": expiry,
        "click_count": 3,
    }

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            if sql.lstrip().startswith("INSERT INTO urls"):
                assert "GREATEST(urls.click_count, EXCLUDED.click_count)" in sql
                assert "urls.org_url = EXCLUDED.org_url" in sql
                sid, org_url, expires_at, click_count, _token_hash = params
                assert sid == state["id"]
                parsed_expiry = datetime.fromisoformat(expires_at) if expires_at else None
                if org_url == state["org_url"] and parsed_expiry == state["expires_at"]:
                    state["click_count"] = max(state["click_count"], click_count)
                    self.rowcount = 1
                else:
                    self.rowcount = 0
                return
            if sql.startswith("SELECT org_url, expires_at, click_count"):
                self._row = (
                    state["org_url"],
                    state["expires_at"],
                    state["click_count"],
                )
                return
            raise AssertionError(f"unexpected SQL: {sql}")

        def fetchone(self):
            return self._row

    class Connection:
        def cursor(self):
            return Cursor()

    class ConnContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(node_app, "_Conn", ConnContext)

    with node_app.app.test_request_context(
        "/node/migrate",
        method="POST",
        json={
            "urls": [
                {
                    "id": state["id"],
                    "org_url": state["org_url"],
                    "expires_at": expiry.isoformat(),
                    "click_count": 9,
                }
            ]
        },
    ):
        response, status = node_app.migrate.__wrapped__()

    assert status == 200
    assert response.get_json() == {"imported": 1, "verified": 1}
    assert state["click_count"] == 9
