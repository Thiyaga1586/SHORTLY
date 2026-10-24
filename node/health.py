"""Dependency-free storage-node liveness probe."""
import os
import urllib.request

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3).close()
