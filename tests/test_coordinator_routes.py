from __future__ import annotations

from dataclasses import dataclass

import coordinator.app as coordinator
from shared.consistent_hash import NodeInfo


@dataclass
class _Response:
    status_code: int
    payload: dict | None = None
    headers: dict | None = None
    content: bytes = b""

    def json(self) -> dict:
        return self.payload or {}


def test_shorten_requires_org_url() -> None:
    client = coordinator.app.test_client()
    response = client.post("/shorten", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "URL is required"


def test_make_short_id_is_seven_base62_characters(monkeypatch) -> None:
    monkeypatch.setattr(coordinator.secrets, "choice", lambda _alphabet: "A")
    assert coordinator.make_short_id() == "AAAAAAA"


def test_shorten_routes_by_generated_id_and_forwards_normalized_url(monkeypatch) -> None:
    node = NodeInfo("node-1", "http://node-1:5001")
    routed_keys: list[str] = []
    forwarded: list[dict] = []
    cached: list[tuple[str, str]] = []

    monkeypatch.setattr(coordinator, "make_short_id", lambda: "Ab3dE9x")
    monkeypatch.setattr(coordinator, "writes_paused", lambda: False)

    def route(key: str):
        routed_keys.append(key)
        return node, None

    def forward(_node, method: str, path: str, **kwargs):
        forwarded.append({"method": method, "path": path, **kwargs})
        return _Response(
            201,
            {
                "short_id": "Ab3dE9x",
                "org_url": "https://example.com/path",
                "expires_at": "2026-10-25T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(coordinator, "_ring_node_or_503", route)
    monkeypatch.setattr(coordinator, "_forward", forward)
    monkeypatch.setattr(coordinator, "cache_set", lambda sid, url, **_kwargs: cached.append((sid, url)))

    client = coordinator.app.test_client()
    response = client.post(
        "/shorten",
        json={"org_url": "  example.com/path  ", "expiry_days": 7},
        headers={"Host": "short.ly"},
    )

    assert response.status_code == 201
    assert response.get_json()["short_url"].endswith("/Ab3dE9x")
    assert routed_keys == ["Ab3dE9x"]
    assert forwarded == [
        {
            "method": "POST",
            "path": "/shorten",
            "json": {
                "org_url": "https://example.com/path",
                "expiry_days": 7,
                "short_id": "Ab3dE9x",
            },
        }
    ]
    assert cached == [("Ab3dE9x", "https://example.com/path")]


def test_shorten_retries_once_when_storage_reports_id_collision(monkeypatch) -> None:
    node = NodeInfo("node-1", "http://node-1:5001")
    ids = iter(["AAAAAAA", "BBBBBBB"])
    routed_keys: list[str] = []
    forwarded_ids: list[str] = []
    cached: list[tuple[str, str]] = []

    monkeypatch.setattr(coordinator, "make_short_id", lambda: next(ids))
    monkeypatch.setattr(coordinator, "writes_paused", lambda: False)

    def route(key: str):
        routed_keys.append(key)
        return node, None

    def forward(_node, _method: str, _path: str, **kwargs):
        short_id = kwargs["json"]["short_id"]
        forwarded_ids.append(short_id)
        if short_id == "AAAAAAA":
            return _Response(409, {"error": "Short ID collision"})
        return _Response(
            201,
            {
                "short_id": short_id,
                "org_url": "https://example.com/retry",
                "expires_at": "2026-10-25T00:00:00+00:00",
            },
        )

    monkeypatch.setattr(coordinator, "_ring_node_or_503", route)
    monkeypatch.setattr(coordinator, "_forward", forward)
    monkeypatch.setattr(coordinator, "cache_set", lambda sid, url, **_kwargs: cached.append((sid, url)))

    response = coordinator.app.test_client().post(
        "/shorten",
        json={"org_url": "example.com/retry", "expiry_days": 1},
    )

    assert response.status_code == 201
    assert response.get_json()["short_url"].endswith("/BBBBBBB")
    assert routed_keys == ["AAAAAAA", "BBBBBBB"]
    assert forwarded_ids == ["AAAAAAA", "BBBBBBB"]
    assert cached == [("BBBBBBB", "https://example.com/retry")]


def test_resolve_cache_hit_does_not_contact_storage(monkeypatch) -> None:
    monkeypatch.setattr(coordinator, "cache_get", lambda _sid: "https://example.com/cached")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("storage lookup should not run on a cache hit")

    monkeypatch.setattr(coordinator, "_ring_node_or_503", unexpected)

    response = coordinator.app.test_client().get("/Ab3dE9x", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "https://example.com/cached"


def test_resolve_cache_miss_routes_by_short_id_and_rewarms_cache(monkeypatch) -> None:
    node = NodeInfo("node-2", "http://node-2:5002")
    routed_keys: list[str] = []
    cached: list[tuple[str, str]] = []

    monkeypatch.setattr(coordinator, "cache_get", lambda _sid: None)

    def route(key: str):
        routed_keys.append(key)
        return node, None

    monkeypatch.setattr(coordinator, "_ring_node_or_503", route)
    monkeypatch.setattr(
        coordinator,
        "_forward",
        lambda *_args, **_kwargs: _Response(
            302,
            headers={"Location": "https://example.com/from-storage"},
        ),
    )
    monkeypatch.setattr(coordinator, "cache_set", lambda sid, url, **_kwargs: cached.append((sid, url)))

    response = coordinator.app.test_client().get("/Ab3dE9x", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "https://example.com/from-storage"
    assert routed_keys == ["Ab3dE9x"]
    assert cached == [("Ab3dE9x", "https://example.com/from-storage")]


def test_forward_get_enforces_end_to_end_deadline_without_real_wait(monkeypatch) -> None:
    node = NodeInfo("node-timeout", "http://node-timeout:5009")
    observed: dict[str, object] = {}

    class FakeFuture:
        def result(self, *, timeout):
            observed["result_timeout"] = timeout
            raise coordinator.FutureTimeoutError()

        def cancel(self):
            observed["cancelled"] = True
            return True

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            observed["submitted_fn"] = fn
            observed["submitted_args"] = args
            observed["submitted_kwargs"] = kwargs
            return FakeFuture()

    monkeypatch.setattr(coordinator, "_node_read_executor", FakeExecutor())
    coordinator._failures.pop(node.node_id, None)

    with coordinator.app.test_request_context(
        "/Ab3dE9x",
        headers={"X-Request-ID": "deadline-test-request"},
    ):
        coordinator._before_request()
        response = coordinator._forward(
            node,
            "GET",
            "/Ab3dE9x",
            allow_redirects=False,
        )

    assert response is None
    assert observed["result_timeout"] == 3.5
    assert observed["cancelled"] is True
    assert observed["submitted_fn"] is coordinator.req_lib.request
    assert observed["submitted_args"] == (
        "GET",
        "http://node-timeout:5009/Ab3dE9x",
    )
    assert observed["submitted_kwargs"] == {
        "timeout": coordinator.REQUEST_TIMEOUT,
        "headers": {"X-Request-ID": "deadline-test-request"},
        "allow_redirects": False,
    }
    assert coordinator._failures[node.node_id][0] == 1
