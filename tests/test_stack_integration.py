from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from urllib.parse import urlsplit

import pytest
import requests


pytestmark = pytest.mark.integration


def _integration_config() -> tuple[str, str]:
    if os.getenv("SHORTLY_INTEGRATION") != "1":
        pytest.skip("set SHORTLY_INTEGRATION=1 to run Docker-stack integration tests")

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    try:
        probe = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Docker daemon is unavailable: {exc}")
    if probe.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    base_url = os.getenv("SHORTLY_BASE_URL", "http://127.0.0.1:5001").rstrip("/")
    api_key = os.getenv("SHORTLY_ADMIN_API_KEY", "")
    if not api_key:
        pytest.skip("SHORTLY_ADMIN_API_KEY is required for stack integration tests")
    return base_url, api_key


def _migration_integration_config() -> tuple[str, str, str]:
    if os.getenv("SHORTLY_MIGRATION_INTEGRATION") != "1":
        pytest.skip(
            "set SHORTLY_MIGRATION_INTEGRATION=1 to run the ring-migration integration test"
        )

    base_url = os.getenv("SHORTLY_MIGRATION_BASE_URL", "").rstrip("/")
    api_key = os.getenv("SHORTLY_MIGRATION_ADMIN_API_KEY", "")
    node4_host = os.getenv("SHORTLY_MIGRATION_NODE4_HOST", "").rstrip("/")
    compose_project = os.getenv("SHORTLY_MIGRATION_COMPOSE_PROJECT", "")
    missing = [
        name
        for name, value in (
            ("SHORTLY_MIGRATION_BASE_URL", base_url),
            ("SHORTLY_MIGRATION_ADMIN_API_KEY", api_key),
            ("SHORTLY_MIGRATION_NODE4_HOST", node4_host),
            ("SHORTLY_MIGRATION_COMPOSE_PROJECT", compose_project),
        )
        if not value
    ]
    if missing:
        pytest.fail(
            "migration integration requires explicit isolated-stack config: "
            + ", ".join(missing)
        )

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or not parsed.port:
        pytest.fail(
            "SHORTLY_MIGRATION_BASE_URL must explicitly target a loopback HTTP port "
            "for an isolated local Compose project"
        )

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    try:
        probe = subprocess.run(
            [
                docker,
                "ps",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                "--format",
                "{{.Ports}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"Docker daemon is unavailable: {exc}")
    if probe.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    port_marker = f":{parsed.port}->5000/tcp"
    if port_marker not in probe.stdout:
        pytest.fail(
            f"{base_url} is not exposed by Compose project {compose_project!r}; "
            "refusing to mutate an unverified or old stack"
        )

    return base_url, api_key, node4_host


def _wait_for_stack(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = requests.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.5)
    pytest.skip(f"Shortly stack is unavailable at {base_url}: {last_error or 'health check failed'}")


def _ensure_three_nodes(session: requests.Session, base_url: str) -> None:
    response = session.get(f"{base_url}/admin/nodes", timeout=5)
    assert response.status_code == 200, response.text
    existing = {node["node_id"] for node in response.json()["nodes"]}

    nodes = [
        ("node-1", "http://node-1:5001"),
        ("node-2", "http://node-2:5002"),
        ("node-3", "http://node-3:5003"),
    ]
    for node_id, host in nodes:
        if node_id in existing:
            continue
        added = session.post(
            f"{base_url}/admin/nodes",
            json={"node_id": node_id, "host": host},
            timeout=30,
        )
        assert added.status_code == 201, added.text


def _create_links(
    session: requests.Session,
    base_url: str,
    *,
    count: int,
    path_prefix: str,
) -> list[tuple[str, str]]:
    run_id = uuid.uuid4().hex
    links: list[tuple[str, str]] = []
    create_failures: list[str] = []

    for i in range(count):
        original = f"https://example.test/{path_prefix}/{run_id}/{i}"
        response = session.post(
            f"{base_url}/shorten",
            json={"org_url": original, "expiry_days": 1},
            timeout=10,
        )
        if response.status_code not in (200, 201):
            create_failures.append(f"#{i}: {response.status_code} {response.text[:200]}")
            continue
        short_id = response.json().get("short_url", "").rsplit("/", 1)[-1]
        if not short_id:
            create_failures.append(f"#{i}: missing short_url in {response.text[:200]}")
            continue
        links.append((short_id, original))

    assert not create_failures, "create failures:\n" + "\n".join(create_failures[:20])
    assert len(links) == count
    return links


def _clear_cache(session: requests.Session, base_url: str) -> None:
    cleared = session.delete(f"{base_url}/admin/cache/clear", timeout=10)
    assert cleared.status_code == 200, cleared.text


def _assert_redirects(
    session: requests.Session,
    base_url: str,
    links: list[tuple[str, str]],
    *,
    context: str,
) -> None:
    redirect_failures: list[str] = []
    server_errors: list[str] = []
    for short_id, original in links:
        response = session.get(
            f"{base_url}/{short_id}",
            allow_redirects=False,
            timeout=10,
        )
        if response.status_code >= 500:
            server_errors.append(f"{short_id}: {response.status_code} {response.text[:160]}")
        if response.status_code != 302 or response.headers.get("Location") != original:
            redirect_failures.append(
                f"{short_id}: status={response.status_code} location={response.headers.get('Location')!r}"
            )

    assert not server_errors, f"5xx responses {context}:\n" + "\n".join(server_errors[:20])
    assert not redirect_failures, f"inconsistent redirects {context}:\n" + "\n".join(
        redirect_failures[:20]
    )


def test_500_links_survive_shared_cache_clear_without_wrong_shard_or_5xx() -> None:
    base_url, api_key = _integration_config()
    _wait_for_stack(base_url)

    session = requests.Session()
    session.headers.update({"X-API-Key": api_key})
    _ensure_three_nodes(session, base_url)

    links = _create_links(
        session,
        base_url,
        count=500,
        path_prefix="shortly-integration",
    )
    _clear_cache(session, base_url)
    _assert_redirects(session, base_url, links, context="after cache clear")


def test_links_survive_add_node4_then_remove_node2_after_cache_clear() -> None:
    base_url, api_key, node4_host = _migration_integration_config()
    _wait_for_stack(base_url)

    session = requests.Session()
    session.headers.update({"X-API-Key": api_key})

    baseline = session.get(f"{base_url}/admin/nodes", timeout=5)
    assert baseline.status_code == 200, baseline.text
    baseline_ids = {node["node_id"] for node in baseline.json()["nodes"]}
    assert baseline_ids == {"node-1", "node-2", "node-3"}, (
        "migration test requires an isolated fresh 3-node ring; "
        f"found {sorted(baseline_ids)}"
    )

    links = _create_links(
        session,
        base_url,
        count=200,
        path_prefix="shortly-migration-integration",
    )

    added = session.post(
        f"{base_url}/admin/nodes",
        json={"node_id": "node-4", "host": node4_host},
        timeout=120,
    )
    assert added.status_code == 201, added.text
    after_add = {node["node_id"] for node in session.get(
        f"{base_url}/admin/nodes", timeout=5
    ).json()["nodes"]}
    assert after_add == {"node-1", "node-2", "node-3", "node-4"}

    _clear_cache(session, base_url)
    _assert_redirects(session, base_url, links, context="after adding node-4 and clearing cache")

    removed = session.delete(f"{base_url}/admin/nodes/node-2", timeout=120)
    assert removed.status_code == 200, removed.text
    after_remove = session.get(f"{base_url}/admin/nodes", timeout=5)
    assert after_remove.status_code == 200, after_remove.text
    remaining_ids = {node["node_id"] for node in after_remove.json()["nodes"]}
    assert remaining_ids == {"node-1", "node-3", "node-4"}

    _clear_cache(session, base_url)
    _assert_redirects(session, base_url, links, context="after removing node-2 and clearing cache")
