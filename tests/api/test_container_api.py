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

    def ping(self):
        return True

    def list_containers(self):
        return [
            {
                "id": "abc",
                "short_id": "abc",
                "name": "demo",
                "image": "demo:latest",
                "running": True,
                "raw_attrs": {"State": {"Running": True}},
            }
        ]

    def logs(self, container_id, tail="all", timestamps=False):
        self.last_logs_container = container_id
        return b"hello\n"

    def get_container(self, container_id):
        return object()

    def get_serialized_container(self, container_id):
        if container_id == "mall-web":
            return {
                "id": "sha256:mall-web-immutable-id",
                "labels": {
                    "com.docker.compose.project": "mall",
                    "com.docker.compose.service": "web",
                },
                "running": True,
            }
        if container_id == "stopped":
            return {
                "id": "sha256:stopped-immutable-id",
                "labels": {},
                "running": False,
            }
        raise ContainerNotFoundError(container_id)

    def list_compose_projects(self):
        return (ComposeProjectRecord("mall", "running(1)", ()),)

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
