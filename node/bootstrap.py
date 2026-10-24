"""Run schema migration exactly once per storage container before Gunicorn."""
import logging
import os
import time
from node.app import init_db


def main():
    if not os.getenv("DATABASE_URL") or not os.getenv("ADMIN_API_KEYS"):
        raise RuntimeError("DATABASE_URL and ADMIN_API_KEYS are required")
    for attempt in range(10):
        try:
            init_db()
            return
        except Exception:
            logging.exception("Schema initialization failed (attempt %d/10)", attempt + 1)
            if attempt == 9:
                raise
            time.sleep(3)


if __name__ == "__main__":
    main()
