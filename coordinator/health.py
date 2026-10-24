"""Dependency-free container liveness probe."""
import urllib.request

if __name__ == "__main__":
    urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=3).close()
