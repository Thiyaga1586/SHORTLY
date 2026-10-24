#!/usr/bin/env python3
"""Concurrent HTTP load test for a running Shortly stack.

The runner creates a seed corpus, optionally clears Redis through the internal
admin endpoint, and drives a Zipf-like redirect workload. It records measured
status counts, throughput, and latency percentiles to JSON. It never fills in
the human-facing report template automatically with unverified claims.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import random
import statistics
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests


RESULTS_DIR = Path(__file__).with_name("results")
_thread_local = threading.local()


@dataclass
class Sample:
    status: int
    latency_ms: float
    ok: bool


def _session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _zipf_indices(size: int, count: int, alpha: float, rng: random.Random) -> list[int]:
    weights = [1.0 / ((i + 1) ** alpha) for i in range(size)]
    return rng.choices(range(size), weights=weights, k=count)


def _request_redirect(base_url: str, short_id: str, timeout: float) -> Sample:
    start = time.perf_counter()
    try:
        response = _session().get(
            f"{base_url}/{short_id}",
            allow_redirects=False,
            timeout=timeout,
        )
        ok = response.status_code == 302 and bool(response.headers.get("Location"))
        status = response.status_code
    except requests.RequestException:
        ok = False
        status = 0
    return Sample(status=status, latency_ms=(time.perf_counter() - start) * 1000, ok=ok)


def seed_links(base_url: str, count: int, timeout: float) -> list[str]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short_ids: list[str] = []
    for i in range(count):
        response = requests.post(
            f"{base_url}/shorten",
            json={"org_url": f"https://example.test/load/{run_id}/{i}", "expiry_days": 1},
            timeout=timeout,
        )
        response.raise_for_status()
        short_id = response.json().get("short_url", "").rsplit("/", 1)[-1]
        if not short_id:
            raise RuntimeError(f"seed response {i} did not contain short_url")
        short_ids.append(short_id)
    return short_ids


def clear_cache(base_url: str, api_key: str, timeout: float) -> None:
    response = requests.delete(
        f"{base_url}/admin/cache/clear",
        headers={"X-API-Key": api_key},
        timeout=timeout,
    )
    response.raise_for_status()


def run_redirect_load(
    base_url: str,
    short_ids: list[str],
    requests_count: int,
    concurrency: int,
    timeout: float,
    zipf_alpha: float,
    random_seed: int,
    max_rps: float = 0.0,
) -> dict:
    rng = random.Random(random_seed)
    sequence = _zipf_indices(len(short_ids), requests_count, zipf_alpha, rng)
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for index_no, index in enumerate(sequence):
            # Without a pace limit, nginx intentionally returns 429 above its
            # per-IP resolve budget. An opt-in cap measures normal operations
            # and makes deliberate overload tests separately reproducible.
            if max_rps > 0:
                due = started + index_no / max_rps
                delay = due - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
            futures.append(executor.submit(_request_redirect, base_url, short_ids[index], timeout))
        samples = [future.result() for future in futures]
    elapsed = time.perf_counter() - started

    latencies = [sample.latency_ms for sample in samples]
    statuses = Counter(str(sample.status) if sample.status else "network_error" for sample in samples)
    successful = sum(sample.ok for sample in samples)
    return {
        "requests": len(samples),
        "successful_redirects": successful,
        "success_rate": round(successful / len(samples), 6) if samples else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_rps": round(len(samples) / elapsed, 3) if elapsed else None,
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else None,
            "mean": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": round(max(latencies), 3) if latencies else None,
        },
        "status_counts": dict(sorted(statuses.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Load-test a running Shortly deployment")
    parser.add_argument("--base", default="http://127.0.0.1:5001", help="Coordinator or gateway base URL")
    parser.add_argument("--seed-base", default="", help="Optional internal coordinator base for seeding, avoiding public write rate limits; reads still target --base")
    parser.add_argument("--seed-links", type=int, default=1000, help="Unique links to create before reads")
    parser.add_argument("--requests", type=int, default=10000, help="Redirect requests in the measured phase")
    parser.add_argument("--concurrency", type=int, default=50, help="Concurrent HTTP workers")
    parser.add_argument("--max-rps", type=float, default=0.0, help="Optional request pacing (0 means unlimited); keep below nginx's per-IP 30/s limit for non-overload tests")
    parser.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout in seconds")
    parser.add_argument("--zipf-alpha", type=float, default=1.2, help="Popularity skew; >1 models hot links")
    parser.add_argument("--random-seed", type=int, default=20261024)
    parser.add_argument("--clear-cache", action="store_true", help="Clear shared Redis cache before measured reads")
    parser.add_argument("--admin-api-key", default="", help="Required with --clear-cache")
    parser.add_argument("--output", type=Path, help="JSON result path; default is benchmark/results/")
    args = parser.parse_args()

    if args.seed_links < 1 or args.requests < 1 or args.concurrency < 1 or args.max_rps < 0:
        parser.error("--seed-links, --requests, and --concurrency must be positive")
    if args.clear_cache and not args.admin_api_key:
        parser.error("--admin-api-key is required with --clear-cache")

    base_url = args.base.rstrip("/")
    seed_base_url = (args.seed_base or base_url).rstrip("/")
    health = requests.get(f"{base_url}/health", timeout=args.timeout)
    health.raise_for_status()

    short_ids = seed_links(seed_base_url, args.seed_links, args.timeout)
    if args.clear_cache:
        clear_cache(base_url, args.admin_api_key, args.timeout)

    metrics = run_redirect_load(
        base_url=base_url,
        short_ids=short_ids,
        requests_count=args.requests,
        concurrency=args.concurrency,
        timeout=args.timeout,
        zipf_alpha=args.zipf_alpha,
        random_seed=args.random_seed,
        max_rps=args.max_rps,
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": base_url,
        "config": {
            "seed_links": args.seed_links,
            "seed_base": seed_base_url,
            "requests": args.requests,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout,
            "zipf_alpha": args.zipf_alpha,
            "random_seed": args.random_seed,
            "cache_cleared_before_reads": args.clear_cache,
            "max_rps": args.max_rps,
        },
        "result": metrics,
    }

    output = args.output
    if output is None:
        RESULTS_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = RESULTS_DIR / f"load_test_{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"\nSaved measured results to {output}")
    return 0 if metrics["success_rate"] == 1.0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
