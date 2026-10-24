#!/bin/sh
set -eu
: "${ADMIN_API_KEYS:?ADMIN_API_KEYS is required}"
: "${DATABASE_URL:?DATABASE_URL is required}"
python -m node.bootstrap
python -m node.cleanup &
exec gunicorn \
    --bind "0.0.0.0:${PORT:-5001}" \
    --workers 1 --threads 2 --worker-class gthread \
    --timeout 30 --keep-alive 5 --log-level info --access-logfile - node.app:app
