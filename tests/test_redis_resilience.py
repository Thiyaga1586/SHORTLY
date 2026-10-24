from __future__ import annotations

import json

import pytest
import redis

from shared import redis_cache, redis_health, ring_store


def test_redis_failure_opens_circuit_and_suppresses_repeat_cache_attempts(monkeypatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(redis_health.time, "monotonic", lambda: clock["now"])
    redis_health.recovered()

    calls = {"count": 0}

    def unavailable_client():
        calls["count"] += 1
        raise redis.ConnectionError("redis unavailable")

    monkeypatch.setattr(redis_cache, "_client", unavailable_client)

    assert redis_cache.cache_get("Ab3dE9x") is None
    assert calls["count"] == 1
    assert redis_health.blocked() is True

    # Once the first failure opens the shared process-local circuit, another
    # cache lookup must fall through without another Redis/DNS attempt.
    assert redis_cache.cache_get("Bc4eF0y") is None
    assert calls["count"] == 1

    clock["now"] += 5.01
    assert redis_health.blocked() is False


def test_redis_health_recovery_closes_circuit_immediately(monkeypatch) -> None:
    clock = {"now": 250.0}
    monkeypatch.setattr(redis_health.time, "monotonic", lambda: clock["now"])
    redis_health.recovered()

    redis_health.failed()
    assert redis_health.blocked() is True

    redis_health.recovered()
    assert redis_health.blocked() is False


def test_open_redis_circuit_prevents_ring_load_from_touching_client(monkeypatch) -> None:
    monkeypatch.setattr(redis_health, "blocked", lambda: True)

    def unexpected_client():
        raise AssertionError("ring_store must not contact Redis while the circuit is open")

    monkeypatch.setattr(ring_store, "_client", unexpected_client)

    try:
        ring_store.load_ring()
    except redis.ConnectionError as exc:
        assert "circuit is open" in str(exc)
    else:
        raise AssertionError("load_ring should fail closed while the Redis circuit is open")


def test_load_ring_reads_nodes_and_version_atomically_with_one_mget(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    recovered: list[bool] = []

    class FakeRedis:
        def mget(self, *keys):
            calls.append(keys)
            return [json.dumps([{"node_id": "node-1", "host": "http://node-1:5001"}]), "12"]

        def get(self, _key):
            raise AssertionError("load_ring must not split ring/version across separate GETs")

    monkeypatch.setattr(redis_health, "blocked", lambda: False)
    monkeypatch.setattr(redis_health, "recovered", lambda: recovered.append(True))
    monkeypatch.setattr(ring_store, "_client", lambda: FakeRedis())

    nodes, version = ring_store.load_ring()

    assert calls == [(ring_store.RING_KEY, ring_store.VERSION_KEY)]
    assert nodes == [{"node_id": "node-1", "host": "http://node-1:5001"}]
    assert version == 12
    assert recovered == [True]


def test_load_ring_redis_error_opens_shared_circuit(monkeypatch) -> None:
    clock = {"now": 500.0}
    monkeypatch.setattr(redis_health.time, "monotonic", lambda: clock["now"])
    redis_health.recovered()

    class BrokenRedis:
        def mget(self, *_keys):
            raise redis.ConnectionError("mget failed")

    monkeypatch.setattr(ring_store, "_client", lambda: BrokenRedis())

    with pytest.raises(redis.ConnectionError, match="mget failed"):
        ring_store.load_ring()

    assert redis_health.blocked() is True


def test_cache_snapshot_throttles_scan_for_five_seconds_and_falls_back_on_outage(monkeypatch) -> None:
    clock = {"now": 100.0}
    pipeline_calls = {"count": 0}
    scan_calls = {"count": 0}
    recovered: list[bool] = []
    failed: list[bool] = []

    class FakePipeline:
        def __init__(self, should_fail: bool):
            self.should_fail = should_fail

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _key):
            return self

        def execute(self):
            if self.should_fail:
                raise redis.ConnectionError("snapshot redis outage")
            return ["4", "1", "2"]

    class FakeRedis:
        fail = False

        def pipeline(self, transaction=False):
            assert transaction is False
            pipeline_calls["count"] += 1
            return FakePipeline(self.fail)

        def scan_iter(self, *, match, count):
            assert match == f"{redis_cache.CACHE_KEY_PFX}*"
            assert count == 200
            scan_calls["count"] += 1
            return iter(["shortly:url:a", "shortly:url:b"])

    client = FakeRedis()
    monkeypatch.setattr(redis_cache, "_last_snapshot", None)
    monkeypatch.setattr(redis_cache, "_snapshot_checked_at", 0.0)
    monkeypatch.setattr(redis_cache.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(redis_cache, "_client", lambda: client)
    monkeypatch.setattr(redis_health, "blocked", lambda: False)
    monkeypatch.setattr(redis_health, "recovered", lambda: recovered.append(True))
    monkeypatch.setattr(redis_health, "failed", lambda: failed.append(True))

    first = redis_cache.cache_snapshot(10)
    assert first.size == 2
    assert first.hits == 4
    assert first.misses == 1
    assert first.evictions == 2
    assert first.hit_rate == 0.8
    assert first.fill_ratio == 0.2
    assert pipeline_calls["count"] == 1
    assert scan_calls["count"] == 1
    assert recovered == [True]

    # Within the five-second window, the cached snapshot is returned without
    # another Redis pipeline or SCAN.
    clock["now"] = 104.99
    second = redis_cache.cache_snapshot(10)
    assert second is first
    assert pipeline_calls["count"] == 1
    assert scan_calls["count"] == 1

    # Once the throttle window expires, a Redis outage must preserve the last
    # good snapshot rather than failing the dashboard/admin stats path.
    clock["now"] = 105.01
    client.fail = True
    fallback = redis_cache.cache_snapshot(10)
    assert fallback is first
    assert pipeline_calls["count"] == 2
    assert scan_calls["count"] == 1
    assert failed == [True]
