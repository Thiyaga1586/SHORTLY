"""Copy and verify records BEFORE changing the consistent-hash ring.

No source record is deleted until callers have persisted the new ring and
waited for coordinator caches to refresh. A failed copy leaves the old ring
serving its original data, so retries are safe and idempotent.
"""

import logging
import requests

log = logging.getLogger(__name__)
TIMEOUT = (1, 15)


class MigrationFailure(Exception):
    pass


def migrate_ring(old_nodes, new_ring, api_key: str, heartbeat=None,
                 skip_unreachable_source_id=None):
    """One complete export per previous owner, batched import by new owner.

    Returns (summary, source-id->ids-to-prune). The caller MUST NOT flip
    the ring if this raises. Works for add, remove and 64-bit wraparound
    because every row is routed using its *short ID*, not token intervals.
    """
    summary = {"exported": 0, "moved": 0, "imported": 0, "verified": 0,
               "unavailable_sources": []}
    pruning = {}
    headers = {"X-API-Key": api_key}
    for source_id, source in old_nodes.items():
        if heartbeat:
            heartbeat()
        try:
            response = requests.get(f"{source.host}/node/export", params={"all": "1"},
                                    headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            records = response.json()["urls"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            if source_id == skip_unreachable_source_id:
                # `force=true` tolerates ONLY failure to export from the
                # explicitly removed node. Every surviving node still has
                # to export and all imports still have to verify, or the
                # membership change is rejected before the ring flips.
                summary["unavailable_sources"].append(source_id)
                log.warning("Forced removal: %s may lose links: %s", source_id, exc)
                continue
            raise MigrationFailure(f"Cannot export from {source_id}: {exc}") from exc
        summary["exported"] += len(records)
        if heartbeat:
            heartbeat()
        batches = {}
        for entry in records:
            target = new_ring.get_node(entry["id"])
            if target is None:
                raise MigrationFailure("Cannot rebalance onto an empty ring")
            if target.node_id != source_id:
                batches.setdefault(target.node_id, (target, []))[1].append(entry)
        for target_id, (target, entries) in batches.items():
            # Bounded POST size, even if a shard grows well beyond demo size.
            for offset in range(0, len(entries), 500):
                if heartbeat:
                    heartbeat()
                batch = entries[offset:offset + 500]
                try:
                    result = requests.post(f"{target.host}/node/migrate",
                                           json={"urls": batch}, headers=headers,
                                           timeout=TIMEOUT)
                    result.raise_for_status()
                    counts = result.json()
                except (requests.RequestException, ValueError) as exc:
                    raise MigrationFailure(f"Cannot import {source_id} -> {target_id}: {exc}") from exc
                if counts.get("verified") != len(batch):
                    raise MigrationFailure(
                        f"Unverified copy {source_id} -> {target_id}: "
                        f"{counts.get('verified')} of {len(batch)}")
                summary["moved"] += len(batch)
                summary["imported"] += counts.get("imported", 0)
                summary["verified"] += counts["verified"]
                pruning.setdefault(source_id, []).extend(x["id"] for x in batch)
    return summary, pruning


def prune_moved(old_nodes, pruning, api_key):
    """Best-effort duplicate cleanup AFTER flip; safe to retry or skip."""
    warnings = []
    for source_id, ids in pruning.items():
        for offset in range(0, len(ids), 500):
            try:
                response = requests.post(f"{old_nodes[source_id].host}/node/prune",
                                         json={"ids": ids[offset:offset + 500]},
                                         headers={"X-API-Key": api_key}, timeout=TIMEOUT)
                response.raise_for_status()
            except requests.RequestException as exc:
                warning = f"Old copies on {source_id} remain: {exc}"
                warnings.append(warning)
                log.warning(warning)
    return warnings
