"""
consistent_hash.py
------------------
Self-contained consistent hashing ring.

Design decisions:
  - SHA-256 for uniform distribution (avoids MD5 clustering on short keys)
  - Virtual nodes (vnodes) to prevent hot spots when nodes have different weights
  - Thread-safe: all mutations hold a RW-aware lock (read-prefer)
  - O(log N) lookup via bisect on a sorted list of token positions
  - Node add/remove returns the set of key ranges that must be migrated
    so callers can move data without a full scan.
"""

import hashlib
import bisect
import threading
from dataclasses import dataclass, field
from typing import Optional


# ── data structures ──────────────────────────────────────────────────────────

@dataclass
class NodeInfo:
    node_id: str          # e.g. "node-1"
    host: str             # e.g. "http://node1:5001"
    weight: int = 1       # relative capacity; heavier node gets more vnodes
    vnodes: int = 150     # base vnode count (multiplied by weight)
    tags: dict = field(default_factory=dict)   # arbitrary metadata


@dataclass
class RingStats:
    total_nodes: int
    total_vnodes: int
    vnode_distribution: dict   # node_id -> vnode_count
    load_std_dev: float        # std-dev of vnode counts (lower = better balance)


# ── ring ─────────────────────────────────────────────────────────────────────

class ConsistentHashRing:
    """
    Consistent hash ring with virtual nodes.

    Usage
    -----
    ring = ConsistentHashRing(vnodes_per_node=150)
    ring.add_node(NodeInfo("node-1", "http://node1:5001"))
    ring.add_node(NodeInfo("node-2", "http://node2:5001"))

    node = ring.get_node("some-short-id")   # -> NodeInfo | None
    replicas = ring.get_nodes("key", n=2)   # -> list[NodeInfo]
    """

    def __init__(self, vnodes_per_node: int = 150):
        self._vnodes_per_node = vnodes_per_node
        self._ring: dict[int, str] = {}       # token -> node_id
        self._sorted_keys: list[int] = []     # sorted token list for bisect
        self._nodes: dict[str, NodeInfo] = {} # node_id -> NodeInfo
        self._lock = threading.RLock()

    # ── hashing ──────────────────────────────────────────────────────────────

    @staticmethod
    def _hash(key: str) -> int:
        """64-bit token from SHA-256 of key string."""
        return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (2 ** 64)

    def _vnode_key(self, node_id: str, replica_index: int) -> str:
        return f"{node_id}::vnode::{replica_index}"

    # ── mutation ─────────────────────────────────────────────────────────────

    def add_node(self, node: NodeInfo) -> list[tuple[int, int]]:
        """
        Add a node and its virtual nodes to the ring.

        Returns a list of (start_token, end_token) ranges this node now owns
        so the caller can migrate data from the previous owner.
        """
        with self._lock:
            if node.node_id in self._nodes:
                raise ValueError(f"Node {node.node_id!r} already in ring")

            self._nodes[node.node_id] = node
            count = self._vnodes_per_node * max(1, node.weight)
            migrated_ranges: list[tuple[int, int]] = []

            for i in range(count):
                token = self._hash(self._vnode_key(node.node_id, i))
                self._ring[token] = node.node_id

                # find what range this new vnode steals from its successor
                pos = bisect.bisect_left(self._sorted_keys, token)
                if self._sorted_keys:
                    if pos == 0:
                        prev_token = self._sorted_keys[-1]
                    else:
                        prev_token = self._sorted_keys[pos - 1]
                    migrated_ranges.append((prev_token, token))

                bisect.insort(self._sorted_keys, token)

            return migrated_ranges

    def remove_node(self, node_id: str) -> list[tuple[int, int]]:
        """
        Remove a node.  Returns ranges the node owned (now reassigned).
        """
        with self._lock:
            if node_id not in self._nodes:
                raise KeyError(f"Node {node_id!r} not in ring")

            node = self._nodes.pop(node_id)
            count = self._vnodes_per_node * max(1, node.weight)
            freed_ranges: list[tuple[int, int]] = []

            for i in range(count):
                token = self._hash(self._vnode_key(node_id, i))
                if token in self._ring:
                    del self._ring[token]
                    pos = bisect.bisect_left(self._sorted_keys, token)
                    if pos < len(self._sorted_keys) and self._sorted_keys[pos] == token:
                        freed_ranges.append((token, self._sorted_keys[(pos + 1) % len(self._sorted_keys)] if self._sorted_keys else token))
                        self._sorted_keys.pop(pos)

            return freed_ranges

    # ── lookup ───────────────────────────────────────────────────────────────

    def get_node(self, key: str) -> Optional[NodeInfo]:
        """Return the primary node responsible for key."""
        with self._lock:
            if not self._sorted_keys:
                return None
            token = self._hash(key)
            pos = bisect.bisect_right(self._sorted_keys, token) % len(self._sorted_keys)
            node_id = self._ring[self._sorted_keys[pos]]
            return self._nodes[node_id]

    def get_nodes(self, key: str, n: int = 2) -> list[NodeInfo]:
        """
        Return up to n distinct nodes for replication.
        Walks clockwise from the key's position, skipping duplicate node_ids.
        """
        with self._lock:
            if not self._sorted_keys:
                return []

            token = self._hash(key)
            pos = bisect.bisect_right(self._sorted_keys, token) % len(self._sorted_keys)

            seen_ids: set[str] = set()
            result: list[NodeInfo] = []
            total = len(self._sorted_keys)

            for i in range(total):
                idx = (pos + i) % total
                node_id = self._ring[self._sorted_keys[idx]]
                if node_id not in seen_ids:
                    seen_ids.add(node_id)
                    result.append(self._nodes[node_id])
                if len(result) == n:
                    break

            return result

    def get_all_nodes(self) -> list[NodeInfo]:
        with self._lock:
            return list(self._nodes.values())

    # ── diagnostics ──────────────────────────────────────────────────────────

    def stats(self) -> RingStats:
        with self._lock:
            dist: dict[str, int] = {}
            for node_id in self._nodes:
                dist[node_id] = 0
            for node_id in self._ring.values():
                dist[node_id] = dist.get(node_id, 0) + 1

            counts = list(dist.values())
            if counts:
                mean = sum(counts) / len(counts)
                variance = sum((c - mean) ** 2 for c in counts) / len(counts)
                std_dev = variance ** 0.5
            else:
                std_dev = 0.0

            return RingStats(
                total_nodes=len(self._nodes),
                total_vnodes=len(self._ring),
                vnode_distribution=dist,
                load_std_dev=round(std_dev, 2),
            )

    def __repr__(self):
        s = self.stats()
        return f"<ConsistentHashRing nodes={s.total_nodes} vnodes={s.total_vnodes} std_dev={s.load_std_dev}>"
