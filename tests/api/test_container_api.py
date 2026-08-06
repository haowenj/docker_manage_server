from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.docker_runtime import ContainerNotRunningError
from docker_manage_server.storage import TaskStore


class ContainerApiRuntime:
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
        return b"hello\n"

    def get_container(self, container_id):
        return object()

    def create_terminal(self, container_id, command):
        raise ContainerNotRunningError(container_id)


@pytest.fixture
def client(tmp_path):
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=TaskStore(tmp_path),
        runtime=ContainerApiRuntime(),
    )
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
