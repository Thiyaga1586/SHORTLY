#!/usr/bin/env bash
# scripts/gen_certs.sh
# ---------------------
# Generates a self-signed TLS certificate for local development / staging.
# For production, replace certs/server.crt and certs/server.key with your
# real certificate (Let's Encrypt, ACM, etc.).
#
# Usage:  bash scripts/gen_certs.sh [DOMAIN]
# Default domain: localhost

set -euo pipefail

DOMAIN="${1:-localhost}"
CERT_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$CERT_DIR"

echo "Generating self-signed cert for: $DOMAIN"
echo "Output: $CERT_DIR/server.{crt,key}"

openssl req -x509 -nodes -days 365 \
  -newkey rsa:2048 \
  -keyout "$CERT_DIR/server.key" \
  -out    "$CERT_DIR/server.crt" \
  -subj   "/CN=$DOMAIN/O=Shortly/C=US" \
  -addext "subjectAltName=DNS:$DOMAIN,IP:127.0.0.1"

chmod 600 "$CERT_DIR/server.key"
echo ""
echo "✅ Cert generated:"
openssl x509 -noout -subject -dates -in "$CERT_DIR/server.crt"
echo ""
echo "NOTE: For production, replace with a real cert:"
echo "  sudo certbot certonly --standalone -d $DOMAIN"
echo "  cp /etc/letsencrypt/live/$DOMAIN/fullchain.pem certs/server.crt"
echo "  cp /etc/letsencrypt/live/$DOMAIN/privkey.pem   certs/server.key"
