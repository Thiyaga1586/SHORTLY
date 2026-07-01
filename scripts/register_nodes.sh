#!/usr/bin/env bash
# scripts/register_nodes.sh  (production v2)
# -------------------------------------------
# Registers storage nodes with an internal coordinator (bypasses nginx /admin block).
# Requires ADMIN_API_KEY env var to match the key set in docker-compose.yml.
#
# Usage:  ADMIN_API_KEY=mysecret bash scripts/register_nodes.sh [COORDINATOR_URL]

set -euo pipefail

COORD="${1:-http://localhost:5001}"          # talk directly to coordinator-1, not nginx
API_KEY="${ADMIN_API_KEY:?ADMIN_API_KEY env var must be set}"

echo "Registering nodes with coordinator at $COORD ..."

register_node() {
  local id="$1" host="$2" weight="${3:-1}"
  echo -n "  → $id ($host, weight=$weight) ... "
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$COORD/admin/nodes" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $API_KEY" \
    -d "{\"node_id\":\"$id\",\"host\":\"$host\",\"weight\":$weight}")
  case "$code" in
    201) echo "✓ registered" ;;
    409) echo "⚠ already registered" ;;
    401) echo "✗ unauthorized — check ADMIN_API_KEY" && exit 1 ;;
    *)   echo "✗ failed (HTTP $code)" ;;
  esac
}

echo "Waiting for coordinator..."
for i in $(seq 1 30); do
  if curl -sf "$COORD/health" > /dev/null 2>&1; then
    echo "  Coordinator is up."
    break
  fi
  sleep 2
  [[ $i -eq 30 ]] && echo "  ERROR: coordinator not healthy." && exit 1
done

register_node "node-1" "http://node-1:5001" 1
register_node "node-2" "http://node-2:5002" 1
register_node "node-3" "http://node-3:5003" 1

echo ""
echo "Ring status:"
curl -s -H "X-API-Key: $API_KEY" "$COORD/admin/nodes" | python3 -m json.tool
echo ""
echo "✅ Done. Open https://localhost/ for the dashboard."
