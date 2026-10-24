# Shortly load-test report

`benchmark/load_test.py` drives a real running deployment with a seed corpus and
a concurrent, popularity-skewed redirect workload. The default Zipf alpha of
`1.2` makes a small set of links hot while still exercising the long tail.

Run a warm-cache measurement against a directly exposed coordinator:

```bash
python benchmark/load_test.py --base http://127.0.0.1:5001 --seed-links 1000 --requests 10000 --concurrency 50
```

Run a cold-cache measurement by clearing the shared URL cache after seeding:

```bash
python benchmark/load_test.py --base http://127.0.0.1:5001 --seed-links 1000 --requests 10000 --concurrency 50 --clear-cache --admin-api-key "$ADMIN_API_KEY"
```

The runner writes raw measured JSON under `benchmark/results/`. Keep that file
with any report so the figures can be traced back to one concrete run.

## Environment

- Commit / working-tree identifier: `TBD`
- Host CPU / memory: `TBD`
- Docker / Compose versions: `TBD`
- Python version: `TBD`
- Service replica counts: `TBD`
- Cache size / TTL: `TBD`
- Vnodes per storage node: `TBD`
- Test target (coordinator or nginx): `TBD`
- Test timestamp: `TBD`

## Results

| Scenario | Seed links | Requests | Concurrency | Throughput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Success rate | 5xx count |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Warm shared cache | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| Cache cleared before reads | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

Only replace `TBD` values with output from an actual run. Do not infer missing
latencies or throughput from a different machine or configuration.

## Validation notes

Record any non-302 responses, network errors, container restarts, saturation,
or test changes observed during the run. If a run is interrupted, keep it out
of the comparison table and rerun with the same parameters.
