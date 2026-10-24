FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary and requests include the needed binary/runtime libraries;
# do not depend on apt mirrors just to install curl for health probes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY coordinator/ ./coordinator/
RUN useradd -r -u 10001 shortly
USER shortly

EXPOSE 5000

# One process and eight threads: per-container Prometheus counters represent
# all traffic rather than only the Gunicorn worker a scrape happened to hit.
# Deployment-level redundancy is provided by three coordinator containers.
CMD ["sh", "-c", "test -n \"$ADMIN_API_KEYS\" || exit 1; exec gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 8 --worker-class gthread --timeout 30 --keep-alive 5 --log-level info --access-logfile - coordinator.app:app"]
