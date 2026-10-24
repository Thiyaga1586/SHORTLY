from __future__ import annotations

import coordinator.app as coordinator
from node import app as node_app


def test_coordinator_metrics_endpoint_exposes_dashboard_metric_families() -> None:
    response = coordinator.app.test_client().get("/metrics")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.content_type.startswith("text/plain")
    assert "shortly_http_requests_total" in body
    assert "shortly_http_request_seconds" in body
    assert "shortly_node_errors_total" in body


def test_node_metrics_endpoint_exposes_dashboard_metric_families() -> None:
    response = node_app.app.test_client().get("/metrics")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.content_type.startswith("text/plain")
    assert "shortly_node_requests_total" in body
    assert "shortly_node_request_seconds" in body
