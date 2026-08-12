from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import (
    ComposeProjectRecord,
    ContainerNotFoundError,
    ContainerNotRunningError,
)
from docker_manage_server.storage import TaskStore


class ContainerApiRuntime:
    def __init__(self):
        self.last_logs_container = None
        self.last_terminal_container = None
        self.lifecycle_calls = []
        self.direct_running = True
        self.mall_running = True
        self.mall_exists = True

    def ping(self):
        return True

    def list_containers(self):
        items = [
            {
                "id": "abc",
                "short_id": "abc",
                "name": "demo",
                "image": "demo:latest",
                "running": True,
                "raw_attrs": {"State": {"Running": True}},
            }
        ]
        if self.direct_running is not None:
            items.append(
                {
                    "id": "sha256:direct-immutable-id",
                    "short_id": "direct",
                    "name": "direct",
                    "image": "demo:latest",
                    "running": self.direct_running,
                    "status": "running" if self.direct_running else "exited",
                    "labels": {},
                    "ports": {},
                    "mounts": [],
                    "networks": {},
                }
            )
        if self.mall_exists:
            items.append(
                {
                    "id": "sha256:mall-web-immutable-id",
                    "short_id": "mall-web",
                    "name": "mall-web",
                    "image": "mall/web:latest",
                    "running": self.mall_running,
                    "status": "running" if self.mall_running else "exited",
                    "labels": {
                        "com.docker.compose.project": "mall",
                        "com.docker.compose.service": "web",
                    },
                    "ports": {},
                    "mounts": [],
                    "networks": {},
                }
            )
        return items

    def logs(self, container_id, tail="all", timestamps=False):
        self.last_logs_container = container_id
        return b"hello\n"

    def get_container(self, container_id):
        return object()

    def get_serialized_container(self, container_id):
        if container_id in ("mall-web", "sha256:mall-web-immutable-id"):
            if not self.mall_exists:
                raise ContainerNotFoundError(container_id)
            return {
                "id": "sha256:mall-web-immutable-id",
                "labels": {
                    "com.docker.compose.project": "mall",
                    "com.docker.compose.service": "web",
                },
                "running": self.mall_running,
            }
        if container_id in ("direct", "sha256:direct-immutable-id"):
            if self.direct_running is None:
                raise ContainerNotFoundError(container_id)
            return {
                "id": "sha256:direct-immutable-id",
                "name": "direct",
                "labels": {},
                "running": self.direct_running,
            }
        if container_id == "stopped":
            return {
                "id": "sha256:stopped-immutable-id",
                "labels": {},
                "running": False,
            }
        raise ContainerNotFoundError(container_id)

    def list_compose_projects(self):
        if not self.mall_exists:
            return ()
        status = "running(1)" if self.mall_running else "exited(1)"
        return (ComposeProjectRecord("mall", status, ()),)

    def start_container(self, container_id):
        self.lifecycle_calls.append(("start_container", container_id))
        self.direct_running = True

    def stop_container(self, container_id):
        self.lifecycle_calls.append(("stop_container", container_id))
        self.direct_running = False

    def restart_container(self, container_id):
        self.lifecycle_calls.append(("restart_container", container_id))

    def remove_container(self, container_id):
        self.lifecycle_calls.append(("remove_container", container_id))
        self.direct_running = None

    def start_compose_project(self, project_name):
        self.lifecycle_calls.append(("start_compose_project", project_name))
        self.mall_running = True

    def stop_compose_project(self, project_name):
        self.lifecycle_calls.append(("stop_compose_project", project_name))
        self.mall_running = False

    def restart_compose_project(self, project_name):
        self.lifecycle_calls.append(("restart_compose_project", project_name))

    def remove_compose_project(self, project_name):
        self.lifecycle_calls.append(("remove_compose_project", project_name))
        self.mall_exists = False

    def create_terminal(self, container_id, command):
        self.last_terminal_container = container_id
        raise ContainerNotRunningError(container_id)


@pytest.fixture
def client(tmp_path):
    runtime = ContainerApiRuntime()
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=TaskStore(tmp_path),
        runtime=runtime,
    )
    app.state.test_runtime = runtime
    return TestClient(app)


def test_containers_returns_all_docker_ps_fields(client):
    response = client.get("/api/containers")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["id"] == "abc"
    assert item["image"] == "demo:latest"
    assert item["running"] is True
    assert item["raw_attrs"]["State"]["Running"] is True


def test_logs_returns_docker_output(client):
    response = client.get("/api/containers/abc/logs?tail=100&timestamps=true")
    assert response.status_code == 200
    assert response.text == "hello\n"


def test_terminal_rejects_stopped_container(client):
    with client.websocket_connect("/api/containers/stopped/terminal") as websocket:
        message = websocket.receive_json()
    assert message["error"] == "container_not_running"
    assert client.app.state.test_runtime.last_terminal_container == (
        "sha256:stopped-immutable-id"
    )


def test_compose_logs_validate_project_ownership(client):
    allowed = client.get("/api/compose-projects/mall/containers/mall-web/logs")
    hidden = client.get("/api/compose-projects/other/containers/mall-web/logs")
    assert allowed.status_code == 200
    assert allowed.text == "hello\n"
    assert client.app.state.test_runtime.last_logs_container == (
        "sha256:mall-web-immutable-id"
    )
    assert hidden.status_code == 404


def test_compose_terminal_hides_cross_project_container(client):
    with client.websocket_connect(
        "/api/compose-projects/other/containers/mall-web/terminal"
    ) as websocket:
        message = websocket.receive_json()
    assert message["error"] == "container_not_found"


def test_compose_terminal_uses_validated_immutable_container_id(client):
    with client.websocket_connect(
        "/api/compose-projects/mall/containers/mall-web/terminal"
    ) as websocket:
        message = websocket.receive_json()

    assert message["error"] == "container_not_running"
    assert client.app.state.test_runtime.last_terminal_container == (
        "sha256:mall-web-immutable-id"
    )


def test_container_lifecycle_api_uses_immutable_id_and_explicit_methods(client):
    stopped = client.post("/api/containers/direct/stop")
    started = client.post("/api/containers/direct/start")
    restarted = client.post("/api/containers/direct/restart")
    stopped_again = client.post("/api/containers/direct/stop")
    removed = client.delete("/api/containers/direct")

    assert [
        response.status_code
        for response in (stopped, started, restarted, stopped_again, removed)
    ] == [200, 200, 200, 200, 200]
    assert client.app.state.test_runtime.lifecycle_calls == [
        ("stop_container", "sha256:direct-immutable-id"),
        ("start_container", "sha256:direct-immutable-id"),
        ("restart_container", "sha256:direct-immutable-id"),
        ("stop_container", "sha256:direct-immutable-id"),
        ("remove_container", "sha256:direct-immutable-id"),
    ]
    assert removed.json()["deleted"] is True


def test_container_lifecycle_api_rejects_state_and_compose_ownership(client):
    assert client.delete("/api/containers/direct").status_code == 409
    assert client.post("/api/containers/stopped/stop").status_code == 409
    assert client.post("/api/containers/mall-web/stop").status_code == 404


def test_compose_lifecycle_api_uses_explicit_project_actions(client):
    stopped = client.post("/api/compose-projects/mall/stop")
    started = client.post("/api/compose-projects/mall/start")
    restarted = client.post("/api/compose-projects/mall/restart")
    removed = client.delete("/api/compose-projects/mall")

    assert [response.status_code for response in (stopped, started, restarted, removed)] == [
        200,
        200,
        200,
        200,
    ]
    assert client.app.state.test_runtime.lifecycle_calls[-4:] == [
        ("stop_compose_project", "mall"),
        ("start_compose_project", "mall"),
        ("restart_compose_project", "mall"),
        ("remove_compose_project", "mall"),
    ]
    assert removed.json() == {"deleted": True, "name": "mall"}


def test_compose_lifecycle_api_rejects_state_and_missing_project(client):
    assert client.post("/api/compose-projects/mall/start").status_code == 409
    assert client.post("/api/compose-projects/missing/stop").status_code == 404
