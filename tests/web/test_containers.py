from docker_manage_server.docker_runtime import ContainerNotFoundError, DockerRuntimeError


def test_container_list_is_server_rendered(web_context):
    client, _store, _runtime = web_context
    response = client.get("/containers")
    assert response.status_code == 200
    assert "server" in response.text
    assert "demo/server:1" in response.text
    assert "0.0.0.0:6308" in response.text


def test_container_detail_exposes_logs_and_terminal_targets(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _container_id: type(
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
    response = client.get("/containers/abc123")
    assert response.status_code == 200
    assert 'data-log-url="/api/containers/abc123/logs"' in response.text
    assert 'data-terminal-url="/api/containers/abc123/terminal"' in response.text
    assert 'type="module"' in response.text
    assert "/static/js/terminal.js" in response.text


def test_container_pages_render_html_errors(web_context):
    client, _store, runtime = web_context
    runtime.get_container = lambda _value: (_ for _ in ()).throw(
        ContainerNotFoundError("x")
    )
    missing = client.get("/containers/missing")
    assert missing.status_code == 404
    assert "找不到容器" in missing.text

    runtime.list_containers = lambda: (_ for _ in ()).throw(
        DockerRuntimeError("offline")
    )
    unavailable = client.get("/containers")
    assert unavailable.status_code == 503
    assert "offline" in unavailable.text
