#!/usr/bin/env bash
# Explicit, read-only backups of the logical Shortly databases in one
# PostgreSQL container. This does NOT upload to S3 or schedule anything.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: bash scripts/backup_postgres.sh <postgres-container> <output-directory>" >&2
  exit 2
fi

container="$1"
output_dir="$2"
# Git Bash on Windows otherwise rewrites container-internal /tmp paths into
# Windows paths. Relative host backup paths still work with this disabled.
docker_exec() { MSYS_NO_PATHCONV=1 docker exec "$@"; }
docker_copy() { MSYS_NO_PATHCONV=1 docker cp "$@"; }
docker inspect --type container "$container" >/dev/null
mkdir -p "$output_dir"
chmod 700 "$output_dir" 2>/dev/null || true

# Only application databases. No risk of backing up unrelated DBs in a
# reused PostgreSQL server, and node-4 is included when configured.
databases="$(docker_exec -u postgres "$container" psql -XAt -d postgres -c \
  "SELECT datname FROM pg_database WHERE datname ~ '^shortly_node_[0-9]+$' ORDER BY datname")"
[[ -n "$databases" ]] || { echo "No Shortly databases found" >&2; exit 1; }

tmp=""
cleanup() {
  if [[ -n "$tmp" ]]; then
    docker_exec -u postgres "$container" rm -f "$tmp" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

while IFS= read -r db; do
  [[ "$db" =~ ^shortly_node_[0-9]+$ ]] || { echo "Unexpected DB name" >&2; exit 1; }
  echo "Backing up $db ..."
  tmp="$(docker_exec -u postgres "$container" mktemp "/tmp/${db}.dump.XXXXXX")"
  docker_exec -u postgres "$container" pg_dump -Fc -Z 6 -f "$tmp" "$db"
  docker_copy "$container:$tmp" "$output_dir/$db.dump" >/dev/null
  chmod 600 "$output_dir/$db.dump" 2>/dev/null || true
  cleanup
  tmp=""
done <<< "$databases"

(cd "$output_dir" && sha256sum shortly_node_*.dump > SHA256SUMS)
chmod 600 "$output_dir/SHA256SUMS" 2>/dev/null || true
echo "Backup files and SHA256SUMS written to $output_dir"
