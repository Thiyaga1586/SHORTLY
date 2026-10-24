from __future__ import annotations

from shared.consistent_hash import ConsistentHashRing, NodeInfo


def _node(node_id: str, *, weight: int = 1) -> NodeInfo:
    return NodeInfo(node_id, f"http://{node_id}:5001", weight=weight)


def _assignments(ring: ConsistentHashRing, count: int = 8_000) -> dict[str, str]:
    return {
        f"key-{i}": ring.get_node(f"key-{i}").node_id
        for i in range(count)
    }


def test_empty_ring_and_vnode_distribution() -> None:
    ring = ConsistentHashRing(vnodes_per_node=150)
    assert ring.get_node("anything") is None
    assert ring.get_nodes("anything", n=2) == []

    for i in range(1, 4):
        ring.add_node(_node(f"node-{i}"))

    stats = ring.stats()
    assert stats.total_nodes == 3
    assert stats.total_vnodes == 450
    assert stats.vnode_distribution == {
        "node-1": 150,
        "node-2": 150,
        "node-3": 150,
    }


def test_hash_distribution_is_reasonably_balanced() -> None:
    ring = ConsistentHashRing(vnodes_per_node=150)
    for i in range(1, 4):
        ring.add_node(_node(f"node-{i}"))

    assignments = _assignments(ring, count=20_000)
    counts = {f"node-{i}": 0 for i in range(1, 4)}
    for owner in assignments.values():
        counts[owner] += 1

    # This is deterministic because the ring uses SHA-256. The range is wide
    # enough to catch pathological clustering without asserting ideal 1/3 splits.
    for count in counts.values():
        share = count / len(assignments)
        assert 0.25 <= share <= 0.42


def test_add_node_only_moves_keys_to_the_new_node() -> None:
    ring = ConsistentHashRing(vnodes_per_node=100)
    for i in range(1, 4):
        ring.add_node(_node(f"node-{i}"))

    before = _assignments(ring)
    ring.add_node(_node("node-4"))
    after = _assignments(ring)

    moved = [key for key in before if before[key] != after[key]]
    assert moved
    assert all(after[key] == "node-4" for key in moved)


def test_remove_node_only_moves_keys_owned_by_removed_node() -> None:
    ring = ConsistentHashRing(vnodes_per_node=100)
    for i in range(1, 5):
        ring.add_node(_node(f"node-{i}"))

    before = _assignments(ring)
    ring.remove_node("node-2")
    after = _assignments(ring)

    assert all(owner != "node-2" for owner in after.values())
    for key, old_owner in before.items():
        if old_owner != "node-2":
            assert after[key] == old_owner


def test_weight_controls_vnode_count() -> None:
    ring = ConsistentHashRing(vnodes_per_node=40)
    ring.add_node(_node("small"))
    ring.add_node(_node("large", weight=3))

    stats = ring.stats()
    assert stats.vnode_distribution == {"small": 40, "large": 120}
    assert stats.total_vnodes == 160


def test_lookup_wraps_from_end_of_ring_to_first_token(monkeypatch) -> None:
    ring = ConsistentHashRing(vnodes_per_node=8)
    ring.add_node(_node("node-1"))
    ring.add_node(_node("node-2"))

    first_token = ring._sorted_keys[0]
    max_token = ring._sorted_keys[-1]
    expected_owner = ring._ring[first_token]

    monkeypatch.setattr(ring, "_hash", lambda _key: max_token + 1)
    assert ring.get_node("wraparound").node_id == expected_owner


def test_get_nodes_wraps_and_returns_distinct_nodes() -> None:
    ring = ConsistentHashRing(vnodes_per_node=25)
    for i in range(1, 4):
        ring.add_node(_node(f"node-{i}"))

    owners = ring.get_nodes("replicated-key", n=10)
    assert len(owners) == 3
    assert len({node.node_id for node in owners}) == 3


def test_duplicate_add_and_unknown_remove_are_rejected() -> None:
    import pytest

    ring = ConsistentHashRing(vnodes_per_node=5)
    ring.add_node(_node("node-1"))

    with pytest.raises(ValueError):
        ring.add_node(_node("node-1"))
    with pytest.raises(KeyError):
        ring.remove_node("missing")
