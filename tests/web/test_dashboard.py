from importlib.resources import files

from docker_manage_server.models import TaskStatus


def test_dashboard_renders_tasks_and_containers(web_context):
    client, store, _runtime = web_context
    task = store.create("task-1", "demo.tar.gz")
    task.status = TaskStatus.PENDING_REVIEW
    task.app_name = "demo"
    store.save(task)

    response = client.get("/")

    assert response.status_code == 200
    assert "Docker Manage" in response.text
    assert "demo" in response.text
    assert "server" in response.text
    assert "全部容器" in response.text
    assert "待审核" in response.text
    assert 'href="/deployments"' in response.text
    assert 'href="/containers"' in response.text


def test_dashboard_degrades_when_docker_is_offline(web_context):
    client, _store, runtime = web_context
    runtime.available = False

    response = client.get("/")

    assert response.status_code == 200
    assert "Docker daemon 不可用" in response.text
    assert "暂无部署任务" in response.text
    assert "暂无容器" in response.text


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
