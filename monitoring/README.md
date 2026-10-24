# Shortly monitoring

The monitoring overlay is intended to be merged with the main Compose file so
all monitoring services join the same `shortly-net` network as the application.
It does not hard-code a project-specific Docker network name.

## Required environment

The main stack already requires `REDIS_PASSWORD` and `SHORTLY_DB_PASSWORD`.
Monitoring reuses those credentials for the Redis and PostgreSQL exporters and
adds one required Grafana administrator password:

```bash
export REDIS_PASSWORD='...'
export SHORTLY_DB_PASSWORD='...'
export GRAFANA_ADMIN_PASSWORD='...'
```

Do not place real credentials in `monitoring/` or in the dashboard JSON.

## Start

From the repository root, with the main stack configuration variables already
set:

```bash
docker compose \
  -f docker-compose.yml \
  -f monitoring/docker-compose.monitoring.yml \
  up -d prometheus grafana blackbox-exporter redis-exporter postgres-exporter
```

Prometheus is exposed on `http://localhost:9090` and Grafana on
`http://localhost:3000`.

Grafana provisions the Prometheus datasource and the **Shortly Production
Overview** dashboard automatically. The dashboard file is
`monitoring/grafana-dashboard.json`.

## What is monitored

- Blackbox HTTP probes check all three coordinators, all three storage nodes,
  and the nginx/frontend gateway.
- Coordinator `/metrics` exposes request rate, response status, latency, and
  backend-node failures.
- Storage-node `/metrics` exposes request rate, response status, and latency.
- `redis_exporter` reports Redis process, memory, and client metrics using the
  configured Redis password.
- `postgres_exporter` reports PostgreSQL metrics using the least-privilege
  Shortly application account.

The dashboard queries measured Prometheus series only; it contains no embedded
benchmark or availability numbers.

## Config validation

The checked-in files can be validated without touching the running app stack:

```bash
docker run --rm --entrypoint promtool \
  -v "$PWD/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  prom/prometheus:v2.51.0 \
  check config /etc/prometheus/prometheus.yml

docker run --rm \
  -v "$PWD/monitoring/blackbox.yml:/etc/blackbox_exporter/config.yml:ro" \
  prom/blackbox-exporter:v0.25.0 \
  --config.file=/etc/blackbox_exporter/config.yml --config.check
```
