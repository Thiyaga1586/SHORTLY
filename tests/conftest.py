from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires the Shortly Docker stack and is skipped unless SHORTLY_INTEGRATION=1",
    )
