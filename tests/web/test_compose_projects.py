from docker_manage_server.docker_runtime import ComposeProjectRecord


def compose_container_fixture(project="mall", container_id="mall-web", running=True):
    return {
        "id": container_id,
        "short_id": container_id,
        "name": container_id,
        "image": "mall/web:1",
        "created": "2026-08-01T00:00:00Z",
        "status": "running" if running else "exited",
        "running": running,
        "ports": {"8000/tcp": [{"HostPort": "6308"}]},
        "labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.service": "web",
            "unsafe": "<script>alert(1)</script>",
        },
        "mounts": [
            {"Source": "/srv/data", "Destination": "/data", "Type": "bind"}
        ],
        "networks": {"mall_default": {"IPAddress": "172.20.0.2"}},
    }


def test_compose_project_detail_renders_container_dialog_and_tools(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ("/srv/mall/compose.yaml",)),
    )
    runtime.containers = [compose_container_fixture()]

    response = client.get("/compose-projects/mall")

    assert response.status_code == 200
    assert "mall-web" in response.text
    assert "web" in response.text
    assert 'data-dialog-open="container-dialog-mall-web"' in response.text
    assert 'id="container-dialog-mall-web"' in response.text
    assert 'data-container-dialog="container-mall-web"' in response.text
    assert 'aria-labelledby="container-mall-web-title"' in response.text
    assert 'id="container-mall-web-title"' in response.text
    assert 'href="/compose-projects/mall/containers/mall-web/logs"' in response.text
    assert 'href="/compose-projects/mall/containers/mall-web/terminal"' in response.text
    assert 'href="/containers/mall-web"' not in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    dialog = response.text.split('<dialog id="container-dialog-mall-web"', 1)[1]
    for action in ("/containers/mall-web/start", "/containers/mall-web/stop"):
        assert action not in dialog


def test_running_compose_detail_shows_stop_restart_delete(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ()),
    )
    runtime.containers = [compose_container_fixture()]
    response = client.get("/compose-projects/mall")
    for action in ("stop", "restart", "delete"):
        assert f'action="/compose-projects/mall/{action}"' in response.text
    assert 'action="/compose-projects/mall/start"' not in response.text
    assert "保留命名卷和数据" in response.text


def test_stopped_compose_detail_shows_start_delete(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "exited(1)", ()),
    )
    runtime.containers = [compose_container_fixture(running=False)]
    response = client.get("/compose-projects/mall")
    assert 'action="/compose-projects/mall/start"' in response.text
    assert 'action="/compose-projects/mall/delete"' in response.text
    assert 'action="/compose-projects/mall/stop"' not in response.text
    assert 'action="/compose-projects/mall/restart"' not in response.text


def test_compose_web_actions_redirect_to_detail_or_runtime(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("mall", "running(1)", ()),
    )
    runtime.containers = [compose_container_fixture()]
    stopped = client.post(
        "/compose-projects/mall/stop", follow_redirects=False
    )
    started = client.post(
        "/compose-projects/mall/start", follow_redirects=False
    )
    restarted = client.post(
        "/compose-projects/mall/restart", follow_redirects=False
    )
    deleted = client.post(
        "/compose-projects/mall/delete", follow_redirects=False
    )
    assert stopped.status_code == 303
    assert stopped.headers["location"] == "/compose-projects/mall"
    assert started.status_code == 303
    assert restarted.status_code == 303
    assert deleted.status_code == 303
    assert deleted.headers["location"] == "/runtime"


def test_compose_project_detail_allows_empty_stopped_project(web_context):
    client, _store, runtime = web_context
    runtime.compose_projects = (
        ComposeProjectRecord("empty", "exited(0)", ("/srv/empty/compose.yaml",)),
    )

    response = client.get("/compose-projects/empty")

    assert response.status_code == 200
    assert "暂无容器" in response.text


def test_unknown_compose_project_returns_404(web_context):
    client, _store, _runtime = web_context
    response = client.get("/compose-projects/missing")
    assert response.status_code == 404
    assert "找不到 Compose 项目" in response.text


def test_compose_web_action_maps_compose_inventory_failure_to_503(web_context):
    client, _store, runtime = web_context
    runtime.compose_error = "compose plugin unavailable"

    response = client.post("/compose-projects/mall/stop")

    assert response.status_code == 503
    assert "compose plugin unavailable" in response.text
    assert runtime.lifecycle_calls == []


def test_compose_log_and_terminal_pages_keep_project_context(web_context):
    client, _store, runtime = web_context
    runtime.containers = [compose_container_fixture()]

    logs = client.get("/compose-projects/mall/containers/mall-web/logs")
    terminal = client.get("/compose-projects/mall/containers/mall-web/terminal")

    assert logs.status_code == 200
    assert (
        'data-log-url="/api/compose-projects/mall/containers/mall-web/logs"'
        in logs.text
    )
    assert 'href="/compose-projects/mall"' in logs.text
    assert "mall / web / mall-web" in logs.text
    assert terminal.status_code == 200
    assert (
        'data-terminal-url="/api/compose-projects/mall/containers/mall-web/terminal"'
        in terminal.text
    )
    assert 'href="/compose-projects/mall"' in terminal.text


def test_compose_tool_pages_hide_cross_project_container(web_context):
    client, _store, runtime = web_context
    runtime.containers = [compose_container_fixture("secret-project", "hidden-web")]
    for suffix in ("logs", "terminal"):
        response = client.get(
            f"/compose-projects/mall/containers/hidden-web/{suffix}"
        )
        assert response.status_code == 404
        assert "找不到容器" in response.text
        assert "secret-project" not in response.text
