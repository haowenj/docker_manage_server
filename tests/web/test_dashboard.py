from importlib.resources import files

from docker_manage_server.docker_runtime import ComposeProjectRecord
from docker_manage_server.models import TaskStatus


def test_dashboard_renders_three_runtime_modules_with_five_items(web_context):
    client, store, runtime = web_context
    for index in range(6):
        task = store.create(f"task-{index}", f"task-{index}.tar.gz")
        task.status = TaskStatus.PENDING_REVIEW
        task.app_name = f"task-app-{index}"
        store.save(task)
    runtime.compose_projects = tuple(
        ComposeProjectRecord(f"project-{index}", "running(1)", ())
        for index in range(6)
    )
    runtime.containers = [
        {
            "id": f"direct-{index}",
            "short_id": f"direct-{index}",
            "name": f"direct-{index}",
            "image": "demo:latest",
            "created": f"2026-08-{index + 1:02d}T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        }
        for index in range(6)
    ]

    response = client.get("/")

    assert response.status_code == 200
    assert "最近部署任务" in response.text
    assert "Compose 项目" in response.text
    assert "独立容器" in response.text
    assert response.text.count("data-dashboard-task-row") == 5
    assert response.text.count("data-dashboard-compose-row") == 5
    assert response.text.count("data-dashboard-container-row") == 5
    assert 'href="/runtime"' in response.text


def test_dashboard_degrades_when_docker_is_offline(web_context):
    client, _store, runtime = web_context
    runtime.available = False

    response = client.get("/")

    assert response.status_code == 200
    assert "Docker daemon 不可用" in response.text
    assert "暂无部署任务" in response.text
    assert "暂无 Compose 项目" in response.text
    assert "暂无独立容器" in response.text


def test_dashboard_degrades_only_compose_listing(web_context):
    client, _store, runtime = web_context
    runtime.compose_error = "compose plugin unavailable"
    runtime.containers = [
        {
            "id": "compose-web",
            "short_id": "compose-web",
            "name": "compose-web",
            "image": "demo:latest",
            "created": "2026-08-01T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {"com.docker.compose.project": "demo"},
            "mounts": [],
            "networks": {},
        },
        {
            "id": "direct",
            "short_id": "direct",
            "name": "direct",
            "image": "alpine:3.21",
            "created": "2026-08-02T00:00:00Z",
            "status": "running",
            "running": True,
            "ports": {},
            "labels": {},
            "mounts": [],
            "networks": {},
        },
    ]

    response = client.get("/")

    assert response.status_code == 200
    assert "compose plugin unavailable" in response.text
    assert 'href="/compose-projects/demo"' in response.text
    assert 'href="/containers/direct"' in response.text
    assert 'href="/containers/compose-web"' not in response.text


def test_template_and_static_resources_are_package_data():
    package = files("docker_manage_server")
    assert package.joinpath("templates/base.html").is_file()
    assert package.joinpath("static/css/app.css").is_file()
    assert package.joinpath("static/js/app.js").is_file()


def test_tool_viewports_have_bounded_height_and_internal_scrolling():
    css = (
        files("docker_manage_server")
        .joinpath("static/css/app.css")
        .read_text(encoding="utf-8")
    )

    assert ".log-output-viewport, .terminal-viewport" in css
    assert "height: clamp(320px, 58vh, 720px)" in css
    assert ".log-output-viewport { overflow: auto;" in css
    assert ".terminal-viewport { overflow: hidden;" in css
    assert ".terminal-viewport { min-height: 360px;" not in css
