FROM python:3.11-slim

WORKDIR /app

# psycopg2 needs libpq, curl is required for the Docker healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY node/ ./node/

# 4 workers × 2 threads. DB connection pool is per-worker (safe).
# Documenting all 3 possible ports — actual bind is whichever PORT
# env var the running container is given (5001/5002/5003).
EXPOSE 5001 5002 5003

CMD gunicorn \
    --bind 0.0.0.0:${PORT:-5001} \
    --workers 4 \
    --threads 2 \
    --worker-class gthread \
    --timeout 30 \
    --keep-alive 5 \
    --log-level info \
    --access-logfile - \
    node.app:app

