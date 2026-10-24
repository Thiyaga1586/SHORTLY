#!/usr/bin/env bash
# Proof-of-restorability into a NEW, explicitly named disposable test DB.
# Refuses to overwrite any existing database or use a production DB name.
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "Usage: bash scripts/restore_postgres.sh <postgres-container> <backup-dir> <source-db> <NEW-restore-test-db>" >&2
  exit 2
fi
container="$1"
backup_dir="$2"
source_db="$3"
target_db="$4"
docker_exec() { MSYS_NO_PATHCONV=1 docker exec "$@"; }
docker_copy() { MSYS_NO_PATHCONV=1 docker cp "$@"; }

[[ "$source_db" =~ ^shortly_node_[0-9]+$ ]] || { echo "Refusing non-Shortly source DB" >&2; exit 2; }
[[ "$target_db" =~ ^shortly_restore_test_[a-z0-9_]+$ ]] || { echo "Destination must start shortly_restore_test_" >&2; exit 2; }
[[ -f "$backup_dir/$source_db.dump" && -f "$backup_dir/SHA256SUMS" ]] || {
  echo "Backup dump/checksum missing" >&2; exit 1;
}
(cd "$backup_dir" && sha256sum --check --status SHA256SUMS) || {
  echo "Backup SHA256 validation failed" >&2; exit 1;
}

docker inspect --type container "$container" >/dev/null
exists="$(docker_exec -u postgres "$container" psql -XAt -d postgres -c \
  "SELECT 1 FROM pg_database WHERE datname = '$target_db'")"
[[ -z "$exists" ]] || { echo "Target database already exists; refusing overwrite" >&2; exit 1; }

tmp="$(docker_exec -u postgres "$container" mktemp '/tmp/shortly-restore.dump.XXXXXX')"
cleanup() { docker_exec "$container" rm -f "$tmp" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker_copy "$backup_dir/$source_db.dump" "$container:$tmp" >/dev/null
docker_exec "$container" chown postgres:postgres "$tmp"

# The role is created by db-init. Restore objects as the least-privilege user.
docker_exec -u postgres "$container" createdb -O shortly "$target_db"
docker_exec -u postgres "$container" pg_restore --exit-on-error --no-owner \
  --role=shortly -d "$target_db" "$tmp"

count="$(docker_exec -u postgres "$container" psql -XAt -d "$target_db" -c 'SELECT COUNT(*) FROM urls')"
echo "RESTORE VERIFIED: $source_db -> $target_db; restored $count URL rows"
echo "The new restore-test DB remains available for inspection. No existing DB was changed."
