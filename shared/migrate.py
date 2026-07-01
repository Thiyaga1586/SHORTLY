"""
shared/migrate.py
-----------------
Automatic key-range migration when nodes join or leave the ring.

Previously (v1): add_node() returned migration ranges but nothing ever
acted on them.  Keys that hashed to the new node were silently lost until
clients re-inserted them.

Now (v2):
  - on add_node  → coordinator tells the PREDECESSOR node(s) to stream
                   the affected key ranges to the new node via POST /node/migrate
  - on remove_node → coordinator tells the SUCCESSOR node(s) to absorb
                     the removed node's ranges (already there; just confirms)

Flow for add_node("node-4"):
  1. Ring returns migration_ranges = [(prev_token, new_token), ...]
  2. For each range, we find the OLD owner (predecessor in ring before add)
  3. GET /node/export?start=prev_token&end=new_token  from old owner
  4. POST /node/migrate  with that batch to new owner
  5. DELETE /node/export?start=...&end=...  from old owner (optional cleanup)

This is best-effort: if a source node is down we log and continue.
A proper production system would use a saga / WAL for this.
"""

import logging
import requests
from dataclasses import asdict
from shared.consistent_hash import NodeInfo

log = logging.getLogger(__name__)

TIMEOUT = 10  # seconds per HTTP call


def migrate_ranges(
    migration_ranges: list[tuple[int, int]],
    new_node: NodeInfo,
    ring,                    # ConsistentHashRing — used to find old owners
    all_nodes: dict[str, NodeInfo],
) -> dict:
    """
    Transfer key ranges from their old owners to new_node.

    Returns a summary dict with per-node counts.
    """
    summary = {"imported": 0, "errors": []}

    # Group ranges by predecessor node (old owner before ring mutation)
    # ring.get_node() now returns new_node for these ranges, so we need
    # to find old owners by walking ring._sorted_keys — we pass them in
    # as the `old_owners` mapping computed by the coordinator before mutation.
    #
    # Since we can't look up pre-mutation state here, we call /node/export
    # on EVERY other node with the range; nodes return [] if they own nothing
    # in that range.  Slightly wasteful but correct.

    for start_token, end_token in migration_ranges:
        for node_id, node in all_nodes.items():
            if node_id == new_node.node_id:
                continue
            try:
                export_url = (
                    f"{node.host}/node/export"
                    f"?start={start_token}&end={end_token}"
                )
                resp = requests.get(export_url, timeout=TIMEOUT)
                if resp.status_code != 200:
                    continue
                batch = resp.json().get("urls", [])
                if not batch:
                    continue

                import_resp = requests.post(
                    f"{new_node.host}/node/migrate",
                    json={"urls": batch},
                    timeout=TIMEOUT,
                )
                imported = import_resp.json().get("imported", 0)
                summary["imported"] += imported
                log.info(
                    "Migrated %d URLs from %s → %s (range %d..%d)",
                    imported, node_id, new_node.node_id, start_token, end_token,
                )
            except Exception as exc:
                msg = f"Migration error from {node_id}: {exc}"
                log.error(msg)
                summary["errors"].append(msg)

    return summary
