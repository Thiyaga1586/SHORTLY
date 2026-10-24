from __future__ import annotations

from dataclasses import asdict

import pytest
import requests

import coordinator.app as coordinator
from shared import auth, migrate as migrate_module, ring_store
from shared.consistent_hash import ConsistentHashRing, NodeInfo


@pytest.fixture
def admin_key(monkeypatch) -> str:
    keys = {"unit-test-admin-key"}
    # The route decorator resolves shared.auth.VALID_KEYS at request time while
    # the migration code uses coordinator.VALID_KEYS to authenticate node calls.
    monkeypatch.setattr(auth, "VALID_KEYS", keys)
    monkeypatch.setattr(coordinator, "VALID_KEYS", keys)
    return next(iter(keys))


def test_membership_mutation_rejects_missing_or_bad_admin_key_before_change(monkeypatch) -> None:
    monkeypatch.setattr(auth, "VALID_KEYS", {"expected-secret"})

    def unexpected_change(*_args, **_kwargs):
        raise AssertionError("membership mutation must not run before authentication")

    monkeypatch.setattr(coordinator, "_change_membership", unexpected_change)
    client = coordinator.app.test_client()
    payload = {"node_id": "node-4", "host": "http://node-4:5004"}

    missing = client.post("/admin/nodes", json=payload)
    wrong = client.post("/admin/nodes", json=payload, headers={"X-API-Key": "wrong"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.get_json()["error"] == "Unauthorized"
    assert wrong.get_json()["error"] == "Unauthorized"


def test_membership_mutation_fails_closed_when_admin_keys_are_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(auth, "VALID_KEYS", set())

    def unexpected_change(*_args, **_kwargs):
        raise AssertionError("membership mutation must not run without configured admin keys")

    monkeypatch.setattr(coordinator, "_change_membership", unexpected_change)
    response = coordinator.app.test_client().post(
        "/admin/nodes",
        json={"node_id": "node-4", "host": "http://node-4:5004"},
        headers={"X-API-Key": "anything"},
    )

    assert response.status_code == 503
    assert response.get_json()["error"] == "Admin API keys not configured on server"


def test_membership_change_refuses_when_maintenance_lease_is_owned_elsewhere(
    monkeypatch, admin_key: str
) -> None:
    monkeypatch.setattr(coordinator, "acquire_maintenance", lambda _token: False)

    def unexpected_load():
        raise AssertionError("ring must not be loaded or mutated without the maintenance lease")

    monkeypatch.setattr(coordinator, "load_ring", unexpected_load)

    response = coordinator.app.test_client().post(
        "/admin/nodes",
        json={"node_id": "node-4", "host": "http://node-4:5004"},
        headers={"X-API-Key": admin_key},
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "Concurrent membership change"


def test_membership_cas_conflict_keeps_ring_uncommitted_and_releases_lease(
    monkeypatch, admin_key: str
) -> None:
    existing = NodeInfo("node-1", "http://node-1:5001")
    acquired: list[str] = []
    released: list[str] = []
    refreshed: list[str] = []
    persisted_versions: list[int] = []
    persisted_tokens: list[str] = []

    def acquire(token: str) -> bool:
        acquired.append(token)
        return True

    def persist(_ring, expected_version: int, lease_token: str | None = None):
        persisted_versions.append(expected_version)
        persisted_tokens.append(lease_token or "")
        raise ring_store.RingConflict("Concurrent ring mutation")

    def migrate(*_args, heartbeat=None, **_kwargs):
        assert heartbeat is not None
        heartbeat()
        return {"exported": 0, "moved": 0, "imported": 0, "verified": 0}, {}

    monkeypatch.setattr(coordinator, "acquire_maintenance", acquire)
    monkeypatch.setattr(coordinator, "release_maintenance", released.append)
    monkeypatch.setattr(coordinator, "refresh_maintenance", refreshed.append)
    monkeypatch.setattr(coordinator, "load_ring", lambda: ([asdict(existing)], 7))
    monkeypatch.setattr(
        coordinator.req_lib,
        "get",
        lambda *_args, **_kwargs: type(
            "Probe", (), {"status_code": 200, "json": lambda self: {"node_id": "node-4"}}
        )(),
    )
    monkeypatch.setattr(coordinator, "migrate_ring", migrate)
    monkeypatch.setattr(coordinator, "_persist_ring", persist)
    monkeypatch.setattr(coordinator.time, "sleep", lambda _seconds: None)

    response = coordinator.app.test_client().post(
        "/admin/nodes",
        json={"node_id": "node-4", "host": "http://node-4:5004"},
        headers={"X-API-Key": admin_key},
    )

    assert response.status_code == 409
    assert response.get_json() == {
        "error": "Concurrent ring mutation",
        "ring_unchanged": True,
    }
    assert persisted_versions == [7]
    assert len(acquired) == 1
    assert persisted_tokens == acquired
    assert refreshed == [acquired[0], acquired[0], acquired[0]]
    assert released == acquired


@pytest.mark.parametrize("mode", ["wrong_identity", "unreachable"])
def test_new_node_preflight_failure_never_persists_ring(
    monkeypatch, admin_key: str, mode: str
) -> None:
    existing = NodeInfo("node-1", "http://node-1:5001")
    released: list[str] = []

    monkeypatch.setattr(coordinator, "acquire_maintenance", lambda _token: True)
    monkeypatch.setattr(coordinator, "release_maintenance", released.append)
    monkeypatch.setattr(coordinator, "load_ring", lambda: ([asdict(existing)], 3))

    if mode == "wrong_identity":
        monkeypatch.setattr(
            coordinator.req_lib,
            "get",
            lambda *_args, **_kwargs: type(
                "Probe", (), {"status_code": 200, "json": lambda self: {"node_id": "wrong-node"}}
            )(),
        )
        expected_error = "New node failed health/identity preflight"
    else:
        def unreachable(*_args, **_kwargs):
            raise requests.ConnectionError("node-4 unavailable")

        monkeypatch.setattr(coordinator.req_lib, "get", unreachable)
        expected_error = "New node unavailable or invalid"

    def unexpected_persist(*_args, **_kwargs):
        raise AssertionError("failed preflight must not persist a new ring")

    def unexpected_migrate(*_args, **_kwargs):
        raise AssertionError("failed preflight must not start migration")

    monkeypatch.setattr(coordinator, "_persist_ring", unexpected_persist)
    monkeypatch.setattr(coordinator, "migrate_ring", unexpected_migrate)

    response = coordinator.app.test_client().post(
        "/admin/nodes",
        json={"node_id": "node-4", "host": "http://node-4:5004"},
        headers={"X-API-Key": admin_key},
    )

    assert response.status_code == 409
    assert response.get_json() == {"error": expected_error, "ring_unchanged": True}
    assert len(released) == 1


def test_forced_removal_migration_skips_only_unreachable_removed_source(monkeypatch) -> None:
    old_nodes = {
        "node-1": NodeInfo("node-1", "http://node-1:5001"),
        "node-2": NodeInfo("node-2", "http://node-2:5002"),
    }
    new_ring = ConsistentHashRing(vnodes_per_node=5)
    new_ring.add_node(old_nodes["node-2"])

    class ExportResponse:
        def __init__(self, urls):
            self._urls = urls

        def raise_for_status(self):
            return None

        def json(self):
            return {"urls": self._urls}

    def fake_get(url, **_kwargs):
        if "node-1" in url:
            raise requests.ConnectionError("removed node is dead")
        return ExportResponse([])

    monkeypatch.setattr(migrate_module.requests, "get", fake_get)

    summary, pruning = migrate_module.migrate_ring(
        old_nodes,
        new_ring,
        "admin-key",
        skip_unreachable_source_id="node-1",
    )

    assert summary["unavailable_sources"] == ["node-1"]
    assert summary["exported"] == 0
    assert pruning == {}


def test_forced_removal_migration_rejects_unreachable_survivor(monkeypatch) -> None:
    old_nodes = {
        "node-1": NodeInfo("node-1", "http://node-1:5001"),
        "node-2": NodeInfo("node-2", "http://node-2:5002"),
    }
    new_ring = ConsistentHashRing(vnodes_per_node=5)
    new_ring.add_node(old_nodes["node-2"])

    def fake_get(url, **_kwargs):
        if "node-1" in url:
            raise requests.ConnectionError("removed node is dead")
        raise requests.ConnectionError("surviving node is also dead")

    monkeypatch.setattr(migrate_module.requests, "get", fake_get)

    with pytest.raises(migrate_module.MigrationFailure, match="Cannot export from node-2"):
        migrate_module.migrate_ring(
            old_nodes,
            new_ring,
            "admin-key",
            skip_unreachable_source_id="node-1",
        )


def test_save_ring_expected_version_mismatch_never_enters_transaction(monkeypatch) -> None:
    class FakePipeline:
        def __init__(self):
            self.multi_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def watch(self, *keys):
            assert keys == (ring_store.VERSION_KEY, ring_store.MAINTENANCE_KEY)

        def get(self, key):
            assert key == ring_store.VERSION_KEY
            return "9"

        def multi(self):
            self.multi_called = True

        def set(self, *_args, **_kwargs):
            raise AssertionError("CAS mismatch must not write the ring")

        def incr(self, *_args, **_kwargs):
            raise AssertionError("CAS mismatch must not increment the version")

        def execute(self):
            raise AssertionError("CAS mismatch must not execute a transaction")

    pipe = FakePipeline()

    class FakeRedis:
        def pipeline(self):
            return pipe

    monkeypatch.setattr(ring_store, "_client", lambda: FakeRedis())

    with pytest.raises(ring_store.RingConflict, match="Ring changed from v8 to v9"):
        ring_store.save_ring([], expected_version=8)

    assert pipe.multi_called is False


def test_save_ring_lost_lease_never_enters_transaction(monkeypatch) -> None:
    class FakePipeline:
        def __init__(self):
            self.multi_called = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def watch(self, *keys):
            assert keys == (ring_store.VERSION_KEY, ring_store.MAINTENANCE_KEY)

        def get(self, key):
            if key == ring_store.VERSION_KEY:
                return "4"
            if key == ring_store.MAINTENANCE_KEY:
                return "some-other-owner"
            raise AssertionError(f"unexpected Redis key {key}")

        def multi(self):
            self.multi_called = True

        def set(self, *_args, **_kwargs):
            raise AssertionError("lost lease must not write the ring")

        def incr(self, *_args, **_kwargs):
            raise AssertionError("lost lease must not increment the version")

        def execute(self):
            raise AssertionError("lost lease must not execute a transaction")

    pipe = FakePipeline()

    class FakeRedis:
        def pipeline(self):
            return pipe

    monkeypatch.setattr(ring_store, "_client", lambda: FakeRedis())

    with pytest.raises(ring_store.RingConflict, match="lease was lost"):
        ring_store.save_ring([], expected_version=4, lease_token="ours")

    assert pipe.multi_called is False


def test_refresh_maintenance_fails_closed_if_lease_is_not_owned(monkeypatch) -> None:
    class FakeRedis:
        def eval(self, _script, numkeys, key, token):
            assert numkeys == 1
            assert key == ring_store.MAINTENANCE_KEY
            assert token == "ours"
            return 0

    monkeypatch.setattr(ring_store, "_client", lambda: FakeRedis())

    with pytest.raises(ring_store.RingConflict, match="lease expired or changed"):
        ring_store.refresh_maintenance("ours")
