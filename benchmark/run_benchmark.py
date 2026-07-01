#!/usr/bin/env python3
"""
benchmark/run_benchmark.py
--------------------------
Runs three experiments and prints results + saves CSV/JSON reports.

Experiment 1 — Cache Hit Rate vs Cache Size
  Inserts N URLs, then reads them with a Zipf distribution
  (realistic: a few URLs get most traffic). Varies cache capacity from
  tiny to 100 % and records hit rate at each point.

Experiment 2 — Load Distribution across nodes
  Hashes 100 000 short IDs through the ring and counts how many land
  on each node.  Compares 1 vnode/node vs 150 vnodes/node.

Experiment 3 — Ring Rebalancing on Node Add/Remove
  Shows how many key ranges are migrated when a node joins or leaves,
  and how quickly the std-dev of load drops back to baseline.

Usage
-----
  # Against a live stack:
  python benchmark/run_benchmark.py --base http://localhost:5000

  # Pure local simulation (no HTTP, uses the ring/cache classes directly):
  python benchmark/run_benchmark.py --local
"""

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from shared.consistent_hash import ConsistentHashRing, NodeInfo
from shared.cache import LRUCache

# ── helpers ───────────────────────────────────────────────────────────────────

REPORT_DIR = Path(__file__).parent / "results"
REPORT_DIR.mkdir(exist_ok=True)


def zipf_sample(n: int, alpha: float = 1.2) -> list[int]:
    """Return `n` indices drawn from a Zipf distribution over 0..n-1."""
    probs = [1.0 / ((i + 1) ** alpha) for i in range(n)]
    total = sum(probs)
    probs = [p / total for p in probs]
    population = list(range(n))
    return random.choices(population, weights=probs, k=n * 5)


def progress(msg: str, done: int, total: int):
    pct = done / total * 100
    bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
    print(f"\r  {msg} [{bar}] {pct:5.1f}%", end="", flush=True)


# ── Experiment 1: Hit Rate vs Cache Size ──────────────────────────────────────

def exp1_hit_rate_vs_cache_size(n_urls: int = 2000, n_reads: int = 10000):
    print("\n" + "=" * 60)
    print("Experiment 1 — Cache Hit Rate vs Cache Size")
    print("=" * 60)
    print(f"  URLs in corpus : {n_urls}")
    print(f"  Reads (Zipf)   : {n_reads}")

    # generate fake URL corpus
    urls = [f"https://example.com/page-{i:05d}" for i in range(n_urls)]
    short_ids = [f"id{i:05d}" for i in range(n_urls)]
    url_map = dict(zip(short_ids, urls))   # short_id -> url

    # generate Zipf-weighted read sequence
    indices = zipf_sample(n_urls)
    read_seq = [short_ids[i % n_urls] for i in indices[:n_reads]]

    cache_sizes = [10, 25, 50, 100, 200, 400, 600, 800, 1000, 1500, 2000]
    results = []

    for cap in cache_sizes:
        cache = LRUCache(capacity=cap, default_ttl=None)  # no TTL for clean bench
        for sid in read_seq:
            val = cache.get(sid)
            if val is None:
                # simulate DB fetch
                cache.set(sid, url_map[sid])
        snap = cache.snapshot()
        results.append({
            "cache_size": cap,
            "hit_rate": snap.hit_rate,
            "hits": snap.hits,
            "misses": snap.misses,
            "evictions": snap.evictions,
        })
        print(f"  cache_size={cap:5d}  hit_rate={snap.hit_rate:.3f}  evictions={snap.evictions}")

    # save CSV
    csv_path = REPORT_DIR / "exp1_hit_rate.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader()
        w.writerows(results)
    print(f"\n  ✓ Results saved → {csv_path}")
    return results


# ── Experiment 2: Load Distribution ──────────────────────────────────────────

def exp2_load_distribution(n_keys: int = 100_000):
    print("\n" + "=" * 60)
    print("Experiment 2 — Load Distribution (1 vnode vs 150 vnodes)")
    print("=" * 60)

    node_ids = ["node-1", "node-2", "node-3"]
    keys = [f"short{i}" for i in range(n_keys)]
    results = {}

    for vnodes in [1, 10, 50, 150]:
        ring = ConsistentHashRing(vnodes_per_node=vnodes)
        for nid in node_ids:
            ring.add_node(NodeInfo(nid, f"http://{nid}:5001"))

        counts = {nid: 0 for nid in node_ids}
        for k in keys:
            node = ring.get_node(k)
            if node:
                counts[node.node_id] += 1

        total = sum(counts.values())
        shares = {nid: round(100 * c / total, 2) for nid, c in counts.items()}
        std = ring.stats().load_std_dev

        print(f"\n  vnodes_per_node={vnodes}")
        for nid, share in shares.items():
            bar = "▓" * int(share / 2)
            print(f"    {nid}: {share:5.2f}%  {bar}")
        print(f"    std-dev of vnode counts: {std}")

        results[vnodes] = {"shares": shares, "std_dev": std}

    json_path = REPORT_DIR / "exp2_load_dist.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  ✓ Results saved → {json_path}")
    return results


# ── Experiment 3: Ring Rebalancing ───────────────────────────────────────────

def exp3_rebalancing():
    print("\n" + "=" * 60)
    print("Experiment 3 — Ring Rebalancing on Node Add/Remove")
    print("=" * 60)

    ring = ConsistentHashRing(vnodes_per_node=150)
    steps = []

    def snapshot(event: str):
        s = ring.stats()
        steps.append({"event": event, **asdict(s)})
        dist_str = "  ".join(f"{k}:{v}" for k, v in s.vnode_distribution.items())
        print(f"  [{event:30s}] nodes={s.total_nodes} vnodes={s.total_vnodes} "
              f"std_dev={s.load_std_dev}  dist=[{dist_str}]")

    snapshot("initial (empty)")

    for i in range(1, 4):
        ring.add_node(NodeInfo(f"node-{i}", f"http://node-{i}:5001"))
        snapshot(f"add node-{i}")

    snapshot("stable 3-node cluster")

    # add a heavier node (weight=2 → gets 2× vnodes)
    ring.add_node(NodeInfo("node-4-heavy", "http://node-4:5001", weight=2))
    snapshot("add node-4 (weight=2)")

    # remove a node (simulates failure / scale-down)
    ring.remove_node("node-2")
    snapshot("remove node-2")

    ring.add_node(NodeInfo("node-2-new", "http://node-2-new:5001"))
    snapshot("add node-2-new (replacement)")

    csv_path = REPORT_DIR / "exp3_rebalancing.csv"
    if steps:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=steps[0].keys())
            w.writeheader()
            w.writerows(steps)
    print(f"\n  ✓ Results saved → {csv_path}")
    return steps


# ── Experiment 4: Live HTTP benchmark (if --base is provided) ─────────────────

def exp4_live_benchmark(base_url: str, n: int = 200):
    try:
        import requests
    except ImportError:
        print("  requests not installed, skipping live benchmark")
        return

    print("\n" + "=" * 60)
    print(f"Experiment 4 — Live HTTP benchmark against {base_url}")
    print("=" * 60)

    short_ids = []

    # shorten N URLs
    print(f"  Shortening {n} URLs...")
    for i in range(n):
        try:
            r = requests.post(f"{base_url}/shorten",
                              json={"org_url": f"https://target.example.com/page/{i}",
                                    "expiry_days": 7},
                              timeout=5)
            if r.ok:
                sid = r.json().get("short_url", "").rsplit("/", 1)[-1]
                if sid:
                    short_ids.append(sid)
        except Exception:
            pass
        if i % 20 == 0:
            progress("shorten", i, n)
    print()

    if not short_ids:
        print("  No URLs shortened, aborting live bench.")
        return

    # read with Zipf pattern
    indices = zipf_sample(len(short_ids))
    reads = [short_ids[i % len(short_ids)] for i in indices[:n * 3]]

    latencies = []
    print(f"  Resolving {len(reads)} reads (Zipf)...")
    for j, sid in enumerate(reads):
        t0 = time.perf_counter()
        try:
            requests.get(f"{base_url}/{sid}", allow_redirects=False, timeout=5)
        except Exception:
            pass
        latencies.append((time.perf_counter() - t0) * 1000)
        if j % 30 == 0:
            progress("resolve", j, len(reads))
    print()

    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.50)]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"  Latency  p50={p50:.1f}ms  p95={p95:.1f}ms  p99={p99:.1f}ms")

    # fetch cache stats
    try:
        cs = requests.get(f"{base_url}/admin/cache", timeout=5).json()
        print(f"  Cache hit_rate={cs.get('hit_rate', '?')}  size={cs.get('size')}  cap={cs.get('capacity')}")
    except Exception:
        pass


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shortly Distributed Benchmark")
    parser.add_argument("--local", action="store_true", help="Run simulation-only benchmarks")
    parser.add_argument("--base",  default="http://localhost:5000", help="Coordinator base URL")
    parser.add_argument("--urls",  type=int, default=2000, help="Number of URLs for exp1")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════╗")
    print("║       Shortly Distributed — Benchmark Suite      ║")
    print("╚══════════════════════════════════════════════════╝")

    exp1_hit_rate_vs_cache_size(n_urls=args.urls)
    exp2_load_distribution()
    exp3_rebalancing()

    if not args.local:
        exp4_live_benchmark(args.base)

    print("\n✅ All done!  Results in:", REPORT_DIR)


if __name__ == "__main__":
    main()
