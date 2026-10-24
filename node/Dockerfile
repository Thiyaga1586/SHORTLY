FROM python:3.11-slim

WORKDIR /app

# psycopg2-binary bundles its libpq dependency. Python implements health probes.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY node/ ./node/
RUN chmod +x node/entrypoint.sh && useradd -r -u 10001 shortly
USER shortly

# One worker × two threads, with a bounded 3-connection pool. This also makes
# single-process Prometheus request counters accurate for the entire container.
# node-4 is an optional Docker Compose scale-profile demonstration.
EXPOSE 5001 5002 5003 5004

CMD ["sh", "node/entrypoint.sh"]

