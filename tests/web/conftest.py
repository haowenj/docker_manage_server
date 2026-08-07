from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from docker_manage_server.api import create_app
from docker_manage_server.config import Settings
from docker_manage_server.storage import TaskStore


class WebFakeRuntime:
    def __init__(self):
        self.available = True
        self.containers = [
            {
                "id": "abc123",
                "short_id": "abc123",
                "name": "server",
                "image": "demo/server:1",
                "status": "running",
                "running": True,
                "ports": {"8000/tcp": [{"HostPort": "6308"}]},
                "labels": {},
                "mounts": [],
                "networks": {},
                "raw_attrs": {"State": {"Running": True}},
            }
        ]

    def ping(self):
        return self.available

    def list_containers(self):
        if not self.available:
            from docker_manage_server.docker_runtime import DockerRuntimeError

            raise DockerRuntimeError("daemon offline")
        return self.containers

    def get_container(self, container_id):
        return SimpleNamespace(id=container_id, attrs={})

    def load_image(self, image_tar, cwd):
        return SimpleNamespace(returncode=0, stdout=b"loaded\n", stderr=b"")

    def compose_up(self, cwd):
        return SimpleNamespace(returncode=0, stdout=b"started\n", stderr=b"")

    def create_terminal(self, container_id, command):
        from docker_manage_server.docker_runtime import ContainerNotRunningError

        raise ContainerNotRunningError(container_id)


@pytest.fixture
def web_context(tmp_path):
    runtime = WebFakeRuntime()
    store = TaskStore(tmp_path)
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        store=store,
        runtime=runtime,
    )
    return TestClient(app), store, runtime
