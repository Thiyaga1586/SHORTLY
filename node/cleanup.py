"""One expiry cleanup process per container, not one per Gunicorn worker."""
from node.app import _cleanup

if __name__ == "__main__":
    _cleanup()
