from docker_manage_server.docker_runtime import ContainerNotFoundError, DockerRuntimeError


def _stub_container():
    return type(
        "Container",
        (),
        {
            "id": "abc123",
            "short_id": "abc123",
            "name": "server",
            "status": "running",
            "ports": {"8000/tcp": [{"HostPort": "6308"}]},
            "labels": {"app": "demo"},
            "image": type("Image", (), {"tags": ["demo/server:1"]})(),
            "attrs": {
                "State": {"Running": True},
                "Config": {"Image": "demo/server:1"},
                "Mounts": [],
                "NetworkSettings": {"Networks": {}},
            },
        },
    )()


def test_container_list_is_server_rendered(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers")
    assert response.status_code == 200
    assert "server" in response.text
    assert "demo/server:1" in response.text
    assert "0.0.0.0:6308" in response.text


def test_container_detail_links_to_standalone_tool_pages(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123")

    assert response.status_code == 200
    assert 'href="/containers/abc123/logs"' in response.text
    assert 'href="/containers/abc123/terminal"' in response.text
    assert response.text.count('target="_blank"') == 2
    assert response.text.count('rel="noopener"') == 2
    assert "data-log-viewer" not in response.text
    assert "data-terminal-url" not in response.text
    assert "/static/js/terminal.js" not in response.text
    assert "/static/vendor/xterm/xterm.css" not in response.text


def test_container_log_page_reuses_existing_log_viewer(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123/logs")

    assert response.status_code == 200
    assert 'data-log-url="/api/containers/abc123/logs"' in response.text
    assert "data-log-tail" in response.text
    assert "data-log-timestamps" in response.text
    assert "data-log-refresh" in response.text
    assert "log-output-viewport" in response.text
    assert 'href="/containers/abc123"' in response.text
    assert "/static/js/terminal.js" not in response.text


def test_container_terminal_page_loads_local_xterm(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: _stub_container()

    response = client.get("/containers/abc123/terminal")

    assert response.status_code == 200
    assert 'data-terminal-url="/api/containers/abc123/terminal"' in response.text
    assert 'data-terminal-command="/bin/sh"' in response.text
    assert "terminal-viewport" in response.text
    assert "/static/vendor/xterm/xterm.css" in response.text
    assert 'type="module"' in response.text
    assert "/static/js/terminal.js" in response.text
    assert 'href="/containers/abc123"' in response.text


def test_container_pages_render_html_errors(web_context):
    client, _store, runtime = web_context
    runtime.get_serialized_container = lambda _value: (_ for _ in ()).throw(
        ContainerNotFoundError("x")
    )

    for path in (
        "/containers/missing",
        "/containers/missing/logs",
        "/containers/missing/terminal",
    ):
        missing = client.get(path)
        assert missing.status_code == 404
        assert "找不到容器" in missing.text

    runtime.list_containers = lambda: (_ for _ in ()).throw(
        DockerRuntimeError("offline")
    )
    unavailable = client.get("/containers")
    assert unavailable.status_code == 503
    assert "offline" in unavailable.text

    runtime.get_serialized_container = lambda _value: (_ for _ in ()).throw(
        DockerRuntimeError("offline")
    )
    for path in (
        "/containers/abc123",
        "/containers/abc123/logs",
        "/containers/abc123/terminal",
    ):
        unavailable = client.get(path)
        assert unavailable.status_code == 503
        assert "offline" in unavailable.text


def test_compose_container_is_hidden_from_standalone_pages(web_context):
    client, _store, runtime = web_context
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
        }
    ]

    for path in (
        "/containers/mall-web",
        "/containers/mall-web/logs",
        "/containers/mall-web/terminal",
    ):
        response = client.get(path)
        assert response.status_code == 404
