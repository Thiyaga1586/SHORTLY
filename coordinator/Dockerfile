FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY shared/ ./shared/
COPY coordinator/ ./coordinator/

EXPOSE 5000

# 4 Gunicorn workers, 2 threads each, 30s timeout
# Adjust --workers based on (2 × CPU cores) + 1
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5000", \
     "--workers", "4", \
     "--threads", "2", \
     "--worker-class", "gthread", \
     "--timeout", "30", \
     "--keep-alive", "5", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "coordinator.app:app"]
