from docker_manage_server.docker_runtime import ComposeProjectRecord


def test_runtime_page_lists_compose_projects_and_standalone_containers(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ("/srv/mall/compose.yaml",)),
    )
    runtime.containers = [
        {
            "id": "mall-web",
            "short_id": "mall-web",
            "name": "mall-web",
            "image": "mall/web:1",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {"com.docker.compose.project": "mall"},
            "mounts": [],
            "networks": {},
        },
        {
            "id": "direct",
            "short_id": "direct",
            "name": "direct",
            "image": "alpine:3.21",
            "created": "2026-08-02T00:00:00Z",
            "status": "exited",
            "running": False,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        },
    ]

    response = client.get("/runtime")

    assert response.status_code == 200
    assert 'href="/compose-projects/mall"' in response.text
    assert 'href="/containers/direct"' in response.text
    assert 'href="/containers/mall-web"' not in response.text


def test_old_container_list_redirects_to_runtime(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/runtime"


def test_runtime_page_returns_503_when_docker_is_offline(web_context):
    client, _store, runtime = web_context
    runtime.available = False

    response = client.get("/runtime")

    assert response.status_code == 503
    assert "daemon offline" in response.text


def test_runtime_page_filters_compose_projects_and_standalone_containers_separately(
    web_context,
):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(2)", ("/srv/mall/compose.yaml",)),
        ComposeProjectRecord("shop", "running(1)", ("/srv/shop/compose.yaml",)),
    )
    runtime.containers = [
        {
            "id": "mall-web",
            "short_id": "mall-web",
            "name": "mall-web",
            "image": "mall/web:1",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {
                "com.docker.compose.project": "mall",
                "com.docker.compose.service": "web",
            },
            "mounts": [],
            "networks": {},
        },
        {
            "id": "shop-api",
            "short_id": "shop-api",
            "name": "shop-api",
            "image": "shop/api:1",
            "created": "2026-08-02T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {
                "com.docker.compose.project": "shop",
                "com.docker.compose.service": "api",
            },
            "mounts": [],
            "networks": {},
        },
        {
            "id": "standalone-full-id",
            "short_id": "standalone",
            "name": "utility",
            "image": "alpine:3.21",
            "created": "2026-08-03T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        },
    ]

    response = client.get("/runtime?compose_q=MALL&container_q=alpine")

    assert response.status_code == 200
    assert 'name="compose_q"' in response.text
    assert 'value="MALL"' in response.text
    assert 'name="container_q"' in response.text
    assert 'value="alpine"' in response.text
    assert 'href="/compose-projects/mall"' in response.text
    assert 'href="/compose-projects/shop"' not in response.text
    assert 'href="/containers/standalone-full-id"' in response.text
    assert 'href="/containers/mall-web"' not in response.text
    assert 'name="container_q" value="alpine"' in response.text


def test_runtime_page_filters_compose_container_or_service_and_renders_empty_results(
    web_context,
):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(2)", ("/srv/mall/compose.yaml",)),
    )
    runtime.containers = [
        {
            "id": "mall-web",
            "short_id": "mall-web",
            "name": "mall-web",
            "image": "mall/web:1",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {
                "com.docker.compose.project": "mall",
                "com.docker.compose.service": "web",
            },
            "mounts": [],
            "networks": {},
        },
        {
            "id": "mall-worker",
            "short_id": "mall-worker",
            "name": "mall-worker",
            "image": "python:3.13",
            "created": "2026-08-02T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {
                "com.docker.compose.project": "mall",
                "com.docker.compose.service": "worker",
            },
            "mounts": [],
            "networks": {},
        },
    ]

    response = client.get("/runtime?compose_q=WORKER&container_q=no-match")

    assert response.status_code == 200
    assert 'href="/compose-projects/mall"' in response.text
    assert "暂无匹配的 Compose 项目。" not in response.text
    assert "暂无匹配的独立容器。" in response.text
    assert 'name="compose_q" value="WORKER"' in response.text
    assert 'name="container_q" value="no-match"' in response.text
