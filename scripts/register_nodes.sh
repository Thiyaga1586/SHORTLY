#!/usr/bin/env bash
# scripts/register_nodes.sh  (production v2)
# -------------------------------------------
# Registers storage nodes with an internal coordinator (bypasses nginx /admin block).
# Reads ADMIN_API_KEY from the environment, or from the project .env file.
#
# Usage:  bash scripts/register_nodes.sh [COORDINATOR_URL]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COORD="${1:-http://127.0.0.1:5001}"          # local-only coordinator-1 port

API_KEY="${ADMIN_API_KEY:-}"
if [[ -z "$API_KEY" && -f "$PROJECT_ROOT/.env" ]]; then
  API_KEY="$(sed -n 's/^ADMIN_API_KEY=//p' "$PROJECT_ROOT/.env" | tail -n 1)"
  API_KEY="${API_KEY%\"}"; API_KEY="${API_KEY#\"}"
  API_KEY="${API_KEY%\'}"; API_KEY="${API_KEY#\'}"
fi
[[ -n "$API_KEY" ]] || { echo "ERROR: ADMIN_API_KEY is not set in the environment or .env"; exit 1; }

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
    *)   echo "✗ failed (HTTP $code)"; return 1 ;;
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
curl -fsS -H "X-API-Key: $API_KEY" "$COORD/admin/nodes"
echo ""
echo "✅ Done. Open the PUBLIC_BASE_URL configured in .env."
